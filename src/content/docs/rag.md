---
title: "OpenCoze RAG 实现说明"
date: 2026-09-30
categories:
  - "RAG"
tags:
  - "RAG"
  - "OpenCoze"
---

本文档整理 OpenCoze 项目中知识库 RAG（Retrieval-Augmented Generation）的完整实现，包括文档索引、解析分片、多路召回、重排序与结果打包的全链路。

## 1. 整体架构

OpenCoze 的 RAG 实现分为**索引链路**（写入）和**检索链路**（读取）两条主线，采用 DDD 分层架构：

```
┌─────────────────────────────────────────────────────────┐
│                    Application 层                        │
│   knowledge.go — API 请求转 entity，调用 domain service  │
│   init.go — 初始化 service + 注册 MQ consumer           │
├─────────────────────────────────────────────────────────┤
│                    Domain 层                             │
│   service/knowledge.go — CreateDocument 等核心方法       │
│   service/retrieve.go — 检索编排主流程（eino Chain）     │
│   service/event_handle.go — MQ 消息处理、解析+索引全流程 │
│   service/convert.go — 类型转换映射（d2s/s2d/fMapping）  │
│   service/validate.go — 文档/知识库可写性校验            │
│   processor/ — DocProcessor 接口与实现                   │
│   entity/ — 领域实体、状态、事件、策略定义               │
├─────────────────────────────────────────────────────────┤
│                    Infra 层                              │
│   document/parser/ — 文档解析器（builtin/ppstructure）   │
│   document/searchstore/ — 检索存储（Milvus/ES）          │
│   document/rerank/ — 重排序（RRF/VikingDB）              │
│   document/nl2sql/ — 自然语言转 SQL                      │
│   document/messages2query/ — Query 改写                  │
│   eventbus/ — 消息队列（nsq/kafka/rmq/pulsar/nats）     │
│   ragflow/ — RAGFlow 外部集成                            │
├─────────────────────────────────────────────────────────┤
│                    Config 层                             │
│   api/model/admin/config — Thrift 配置结构               │
│   bizpkg/config/knowledge — 配置加载与默认值回退         │
└─────────────────────────────────────────────────────────┘
```

## 2. 索引链路（写入）

### 2.1 整体流程

文档从上传到可检索，经过以下步骤：

```
[API 请求] CreateDocument
  │
  ├─ 1. BeforeCreate()  — 上传文件到对象存储（TOS）
  ├─ 2. BuildDBModel()  — 生成文档 ID，构建 DB 模型
  ├─ 3. InsertDBModel() — 事务写入数据库（含表格物理表创建）
  ├─ 4. Indexing()      — 发送 IndexDocumentsEvent 到 MQ
  │
  └─ [MQ Consumer 异步处理]
       ├─ indexDocuments     — 批量事件拆为单文档事件
       │    └─ indexDocument  — 单文档索引全流程
       │         ├─ validateDocumentStatus     — 校验可写性
       │         ├─ beginIndexingProcess       — 状态 → Chunking
       │         ├─ processDocumentParsing     — 解析文档
       │         │    └─ parseManager.GetParser → parser.Parse
       │         ├─ handleTableDocument         — 表格特殊处理
       │         ├─ processDocumentChunks       — 批量分片（batch=100）
       │         │    └─ batchProcessSlice
       │         │         ├─ genMultiIDs
       │         │         ├─ convertToSlices     — d2sMapping
       │         │         ├─ upsertDataToTable   — 表类型写 RDB
       │         │         ├─ storeSlicesInDB     — 写 slice 表
       │         │         └─ indexSlicesInSearchStores — 写向量库+ES
       │         │              ├─ manager.Create(collection)
       │         │              └─ ss.Store(ssDocs)
       │         └─ finalizeDocumentIndexing    — 状态 → Enable
```

### 2.2 DocProcessor 接口

[processor/interface.go](../domain/knowledge/processor/interface.go) 定义了文档处理的标准流程：

```go
type DocProcessor interface {
    BeforeCreate() error         // 获取数据源，上传到对象存储
    BuildDBModel() error         // 构建文档记录
    InsertDBModel() error        // 将文档记录插入数据库（事务）
    Indexing() error             // 发起索引任务（发送 MQ 事件）
    GetResp() []*entity.Document // 返回处理后的文档信息
}
```

基础实现在 [processor/impl/base.go](../domain/knowledge/processor/impl/base.go)，其中 `Indexing()` 方法发送事件：

```go
func (p *baseDocProcessor) Indexing() error {
    event := events.NewIndexDocumentsEvent(p.Documents[0].KnowledgeID, p.Documents)
    body, _ := sonic.Marshal(event)
    return p.producer.Send(p.ctx, body)
}
```

### 2.3 事件驱动机制

索引采用**两级事件**机制：

| 事件类型 | 触发场景 | 处理逻辑 |
|----------|----------|----------|
| `IndexDocuments` | CreateDocument 批量提交 | 拆分为多个 `IndexDocument` 事件 |
| `IndexDocument` | 单文档索引 | 解析 → 分片 → 写 DB + SearchStore |
| `IndexSlice` | 单分片增量索引 | 仅写搜索存储 |
| `DeleteKnowledgeData` | 删除知识库 | 清理 SearchStore + DB 数据 |
| `DocumentReview` | 文档预览 | 解析文档返回预览 |

事件定义在 [entity/event.go](../domain/knowledge/entity/event.go)，工厂函数在 [internal/events/events.go](../domain/knowledge/internal/events/events.go)。

消息队列支持 5 种后端（[infra/eventbus/impl/eventbus.go](../infra/eventbus/impl/eventbus.go)）：通过 `MQTypeKey` 环境变量选择 `nsq` / `kafka` / `rmq` / `pulsar` / `nats`。

批量事件拆分时使用 `shardingKey=knowledgeID` 保证同一知识库的事件顺序性：

```go
func (k *knowledgeSVC) indexDocuments(ctx context.Context, event *entity.Event) {
    for i := range event.Documents {
        doc := event.Documents[i]
        e := events.NewIndexDocumentEvent(doc.KnowledgeID, doc)
        msgData, _ := sonic.Marshal(e)
        k.producer.Send(ctx, msgData, eventbus.WithShardingKey(strconv.FormatInt(doc.KnowledgeID, 10)))
    }
}
```

### 2.4 文档解析（Parser）

#### 解析器管理器

[infra/document/parser/manager.go](../infra/document/parser/manager.go) 定义接口：

```go
type Manager interface {
    GetParser(config *Config) (Parser, error)
    IsAutoAnnotationSupported() bool
}
```

`Config` 包含文件扩展名、解析策略和分片策略：

```go
type Config struct {
    FileExtension    FileExtension
    ParsingStrategy  *ParsingStrategy
    ChunkingStrategy *ChunkingStrategy
}
```

#### 支持的文件类型

[builtin/manager.go](../infra/document/parser/impl/builtin/manager.go) 按扩展名分发：

| 扩展名 | 解析方式 |
|--------|----------|
| PDF | Python 脚本（parse_pdf.py），支持 OCR |
| TXT | 直接读取文本 |
| Markdown | Markdown 解析 + OCR |
| DOCX | Python 脚本 |
| CSV | CSV 解析 |
| XLSX | Excel 解析 |
| JSON | JSON 解析 |
| JPG/JPEG/PNG | 图片解析（调用多模态模型） |

另有 [ppstructure/manager.go](../infra/document/parser/impl/ppstructure/manager.go) 基于 PP-StructureV2 的 PDF 解析实现。

#### 分片策略

[entity/strategy.go](../domain/knowledge/entity/strategy.go) 定义：

```go
type ChunkingStrategy struct {
    ChunkType       ChunkType  // Default(0) / Custom(1) / Leveled(2)
    ChunkSize       int64      // 最大分片长度
    Separator       string     // 分隔符
    Overlap         int64      // 重叠比例（百分比）
    TrimSpace       bool
    TrimURLAndEmail bool
    MaxDepth        int64      // 层级分片最大深度
    SaveTitle       bool       // 保留层级标题
}
```

默认分片参数（[internal/consts/consts.go](../domain/knowledge/internal/consts/consts.go)）：

| 参数 | 默认值 |
|------|--------|
| ChunkSize | 800 |
| Separator | `\n` |
| Overlap | 10（即 10%） |

#### 自定义分片算法

[builtin/chunk_custom.go](../infra/document/parser/impl/builtin/chunk_custom.go) 核心逻辑：

1. 按 `Separator` 切分文本
2. 逐段追加到 buffer，达到 `ChunkSize` 时生成分片
3. `Overlap > 0` 时，从上一分片尾部按百分比截取重叠内容
4. 支持 `TrimSpace` 和 `TrimURLAndEmail` 清洗

```go
func getOverlap(runes []rune, overlapRatio int64, chunkSize int64) []rune {
    overlap := int64(float64(chunkSize) * float64(overlapRatio) / 100)
    if int64(len(runes)) <= overlap { return runes }
    return runes[len(runes)-int(overlap):]
}
```

#### 分片批处理

[event_handle.go](../domain/knowledge/service/event_handle.go) 中 `processDocumentChunks` 按 `batchSize=100` 批量处理：

```go
func (k *knowledgeSVC) processDocumentChunks(ctx context.Context, doc *entity.Document,
    parseResult []*schema.Document, cacheRecord *indexDocCacheRecord) error {
    batchSize := 100
    progressbar := progressbar.New(ctx, doc.ID, int64(len(parseResult)*len(k.searchStoreManagers)), k.cacheCli, true)
    for i := int(cacheRecord.LastProcessedNumber); i < len(parseResult); i += batchSize {
        chunks := parseResult[i:min(i+batchSize, len(parseResult))]
        if err := k.batchProcessSlice(ctx, doc, i, chunks, cacheRecord, progressbar); err != nil {
            return err
        }
    }
    return nil
}
```

### 2.5 向量索引写入

#### SearchStore 接口

[searchstore.go](../infra/document/searchstore/searchstore.go) 继承 eino 框架的 Indexer 和 Retriever：

```go
type SearchStore interface {
    indexer.Indexer     // Store 方法 — 写入索引
    retriever.Retriever  // Retrieve 方法 — 检索
    Delete(ctx context.Context, ids []string) error
}
```

#### Manager 工厂

[impl/impl.go](../infra/document/searchstore/impl/impl.go) 返回两个 Manager — ES（全文）+ 向量库：

```go
func New(ctx context.Context, conf *config.KnowledgeConfig, es es.Client) ([]Manager, error) {
    esSearchstoreManager := elasticsearch.NewManager(...)
    mgr, err := getVectorStore(ctx, conf) // milvus / vikingdb / oceanbase
    return []searchstore.Manager{esSearchstoreManager, mgr}, nil
}
```

向量存储后端通过 `VECTOR_STORE_TYPE` 环境变量选择。

#### 索引写入核心

[event_handle.go](../domain/knowledge/service/event_handle.go) 的 `indexSlicesInSearchStores`：

```go
func (k *knowledgeSVC) indexSlicesInSearchStores(ctx context.Context, doc *entity.Document,
    collectionName string, sliceEntities []*entity.Slice, ...) error {

    fields, _ := k.mapSearchFields(doc)           // fMapping 按文档类型定义字段
    indexingFields := getIndexingFields(fields)    // 标记 Indexing=true 的字段
    ssDocs, _ := slices.TransformWithErrorCheck(sliceEntities, func(a *entity.Slice) (*schema.Document, error) {
        return k.slice2Document(ctx, doc, a)       // s2dMapping 转换
    })

    for _, manager := range k.searchStoreManagers {
        manager.Create(ctx, &searchstore.CreateRequest{CollectionName: collectionName, Fields: fields})
        ss, _ := manager.GetSearchStore(ctx, collectionName)
        ss.Store(ctx, ssDocs,
            searchstore.WithIndexerPartitionKey(fieldNameDocumentID),
            searchstore.WithPartition(strconv.FormatInt(doc.ID, 10)),
            searchstore.WithIndexingFields(indexingFields),
            searchstore.WithProgressBar(progressBar),
        )
    }
    return nil
}
```

#### Collection 命名

```go
func getCollectionName(knowledgeID int64) string {
    return fmt.Sprintf("opencoze_%d", knowledgeID)
}
```

每个知识库对应一个 collection，文档 ID 作为 partition key。

#### 字段映射（fMapping）

[convert.go](../domain/knowledge/service/convert.go) 按文档类型定义搜索字段：

| 文档类型 | 索引字段 |
|----------|----------|
| Text | `id`(主键), `creator_id`, `document_id`, `text_content`(indexing) |
| Table | `id`, `creator_id`, `document_id`, + 各列字段(indexing) 或 `text_content`(压缩模式) |
| Image | `id`, `creator_id`, `document_id`, `text_content`(indexing) |

### 2.6 文档状态管理

[entity/const.go](../domain/knowledge/entity/const.go) 定义文档状态：

| 状态 | 值 | 说明 |
|------|----|------|
| Init | -1 | 初始化 |
| Uploading | 0 | 上传中 |
| Enable | 1 | 生效（可检索） |
| Disable | 2 | 失效 |
| Deleted | 3 | 已删除 |
| Chunking | 4 | 切片中 |
| Failed | 9 | 失败 |

索引过程状态流转：

```
Uploading → Chunking → Enable（成功）
                    └→ Failed（不可重试错误）
                    └→ Chunking（可重试错误，等待重试）
```

错误处理逻辑（[event_handle.go](../domain/knowledge/service/event_handle.go)）：
- `ErrKnowledgeNonRetryableCode` → `DocumentStatusFailed`
- 其他错误 → `DocumentStatusChunking`（等待 MQ 重试）
- panic → `DocumentStatusFailed`

### 2.7 Slice 状态

| 状态 | 值 | 说明 |
|------|----|------|
| Init | 0 | 初始化 |
| FinishStore | 1 | SearchStore 存储完成 |
| Failed | 9 | 失败 |

## 3. 检索链路（读取）

### 3.1 整体流程

检索使用 eino 框架的 `compose.Chain` 编排 DAG，节点顺序为 **查询改写 → 并行多路召回 → 重排 → 打包**：

```
Retrieve(ctx, request)
  │
  ├─ newRetrieveContext — 过滤启用知识库/文档，构建 RetrieveContext
  │
  └─ eino Chain 编译并执行:
       │
       1. queryRewriteNode
       │    └─ EnableQueryRewrite 且有对话历史时
       │       调用 rewriter.MessagesToQuery 改写查询
       │
       2. Parallel（并行多路召回）
       │    ├─ passRequestContext — 透传 RetrieveContext
       │    │
       │    ├─ [RAGFlow 模式]
       │    │    └─ ragflowRetrieveNode — 调用 RAGFlow API 检索
       │    │
       │    └─ [原始模式] 三路并行:
       │         ├─ vectorRetrieveNode — 向量检索（Milvus）
       │         │    └─ SearchType != FullText 时生效
       │         ├─ esRetrieveNode — 全文检索（ES）
       │         │    └─ SearchType != Semantic 时生效
       │         └─ nl2SqlRetrieveNode — NL2SQL（表格）
       │              └─ EnableNL2SQL 且有表格文档时生效
       │
       3. reRankNode
       │    └─ 按 SearchType 组装通道 → reranker.Rerank
       │       → MinScore 过滤
       │
       4. packResults
            └─ slice ID 反查 DB → 按文档类型构造 RetrieveSlice
               → 递增命中计数
```

### 3.2 检索编排核心代码

[retrieve.go](../domain/knowledge/service/retrieve.go) 的 `Retrieve` 方法：

```go
func (k *knowledgeSVC) Retrieve(ctx context.Context, request *RetrieveRequest) (response *RetrieveResponse, err error) {
    if len(request.Query) == 0 {
        return &knowledgeModel.RetrieveResponse{}, nil
    }
    retrieveContext, err := k.newRetrieveContext(ctx, request)
    if len(retrieveContext.Documents) == 0 {
        return &knowledgeModel.RetrieveResponse{}, nil
    }

    chain := compose.NewChain[*RetrieveContext, []*knowledgeModel.RetrieveSlice]()
    rewriteNode := compose.InvokableLambda(k.queryRewriteNode)
    passRequestContextNode := compose.InvokableLambda(k.passRequestContext)
    reRankNode := compose.InvokableLambda(k.reRankNode)
    packResult := compose.InvokableLambda(k.packResults)

    parallelNode := compose.NewParallel().
        AddLambda("passRequestContext", passRequestContextNode)

    if k.ragflowEnabled {
        parallelNode.AddLambda("ragflowRetrieveNode", compose.InvokableLambda(k.ragflowRetrieveNode))
    } else {
        parallelNode.
            AddLambda("vectorRetrieveNode", vectorRetrieveNode).
            AddLambda("esRetrieveNode", EsRetrieveNode).
            AddLambda("nl2SqlRetrieveNode", Nl2SqlRetrieveNode)
    }

    r, err := chain.
        AppendLambda(rewriteNode).
        AppendParallel(parallelNode).
        AppendLambda(reRankNode).
        AppendLambda(packResult).
        Compile(ctx)

    output, err := r.Invoke(ctx, retrieveContext)
    return &RetrieveResponse{RetrieveSlices: output}, nil
}
```

### 3.3 检索策略模型

[crossdomain/knowledge/model/knowledge.go](../crossdomain/knowledge/model/knowledge.go) 定义：

```go
type RetrievalStrategy struct {
    TopK              *int64      // 1-10，默认 3
    MinScore          *float64    // 0.01-0.99，默认 0.5
    MaxTokens         *int64
    SelectType        SelectType  // Auto(0) / OnDemand(1)
    SearchType        SearchType  // Semantic(0) / FullText(1) / Hybrid(2)
    EnableQueryRewrite bool
    EnableRerank       bool
    EnableNL2SQL       bool
    IsPersonalOnly     bool
}
```

SearchType 与召回通道的对应关系：

| SearchType | 向量检索 | ES 全文 | NL2SQL |
|------------|----------|---------|--------|
| Semantic (0) | 启用 | 跳过 | 按 EnableNL2SQL |
| FullText (1) | 跳过 | 启用 | 按 EnableNL2SQL |
| Hybrid (2) | 启用 | 启用 | 按 EnableNL2SQL |

### 3.4 查询改写（Query Rewrite）

[infra/document/messages2query](../infra/document/messages2query) 实现，使用 eino Chain 编排：

```
消息列表 → JSON 序列化 → Jinja2 模板填充 → LLM 生成 → 返回改写后的 query
```

模板文件：`resources/conf/prompt/messages_to_query_template_jinja2.json`

触发条件（[retrieve.go](../domain/knowledge/service/retrieve.go) `queryRewriteNode`）：
- `len(ChatHistory) > 1`（有对话历史）
- `Strategy.EnableQueryRewrite == true`
- `rewriter != nil`

改写后的 query 存入 `RetrieveContext.RewrittenQuery`，后续召回节点优先使用。

### 3.5 向量检索（vectorRetrieveNode）

从 `searchStoreManagers` 中筛选 `TypeVectorStore` 的 Manager（Milvus），调用 `retrieveChannels`：

```go
func (k *knowledgeSVC) retrieveChannels(ctx context.Context, req *RetrieveContext,
    manager searchstore.Manager) (result []*schema.Document, err error) {
    query := req.OriginQuery
    if req.Strategy.EnableQueryRewrite && req.RewrittenQuery != nil {
        query = *req.RewrittenQuery
    }
    eg, ctx := errgroup.WithContext(ctx)
    eg.SetLimit(2)                            // 并发上限 2
    for knowledgeID, knowledgeInfo := range req.KnowledgeInfoMap {
        dsl := &searchstore.DSL{              // document_id IN [...] 过滤
            Op: searchstore.OpIn, Field: "document_id", Value: knowledgeInfo.DocumentIDs,
        }
        opts := []retriever.Option{
            searchstore.WithRetrieverPartitionKey(fieldNameDocumentID),
            searchstore.WithPartitions(partitions),
            retriever.WithDSLInfo(dsl.DSL()),
            retriever.WithTopK(8),            // 固定 TopK=8
        }
        eg.Go(func() error {
            ss, _ := manager.GetSearchStore(ctx, collectionName)
            retrievedDocs, _ := ss.Retrieve(ctx, query, opts...)
            mu.Lock(); result = append(result, retrievedDocs...); mu.Unlock()
            return nil
        })
    }
    eg.Wait()
    return
}
```

#### Milvus 检索细节

[milvus_searchstore.go](../infra/document/searchstore/impl/milvus/milvus_searchstore.go)：

- **混合检索**：当 Embedding 支持 dense+sparse 时，使用 `HybridSearch` + 服务端 RRF 融合
- **单路检索**：仅 dense 或仅 sparse 时，使用 `Search`
- **分数归一化**：L2 距离 `1.0-(score-min)/base`，IP/COSINE `(score+1)/2`
- **DSL 转过滤**：`dsl2Expr` 将 DSL 转为 Milvus 布尔表达式（`==`/`!=`/`LIKE`/`IN`/`AND`/`OR`）

#### 向量字段命名约定

| 用途 | 命名规则 |
|------|----------|
| Dense 向量字段 | `dense_{fieldName}` |
| Dense 索引名 | `index_dense_{fieldName}` |
| Sparse 向量字段 | `sparse_{fieldName}` |
| Sparse 索引名 | `index_sparse_{fieldName}` |

#### 索引默认参数

| 参数 | 默认值 |
|------|--------|
| Dense 索引类型 | HNSW (M=30, efConstruction=360) |
| Dense 度量 | IP |
| Sparse 索引类型 | SPARSE_INVERTED_INDEX (drop_ratio=0.2) |
| Sparse 度量 | IP |
| ShardNum | 1 |
| BatchSize | 100 |

### 3.6 全文检索（esRetrieveNode）

从 `searchStoreManagers` 中筛选 `TypeTextStore` 的 Manager（Elasticsearch）。

[elasticsearch_searchstore.go](../infra/document/searchstore/impl/elasticsearch/elasticsearch_searchstore.go)：

- **匹配方式**：无 `MultiMatch` 时用 `MatchQuery(text_content, query)`；有 `MultiMatch` 时用多字段匹配（表格列检索）
- **DSL 转 ES Query**：`travDSL` 将 DSL 映射为 ES 的 `Must`/`MustNot`/`Should`/`Match`/`Equal`/`In`
- **分数归一化**：每条结果分数除以首条分数，归一化到 [0,1]

### 3.7 NL2SQL 检索（nl2SqlRetrieveNode）

[infra/document/nl2sql](../infra/document/nl2sql) 实现，使用 eino Chain 编排：

```
TableSchema → Markdown 表格描述 → Jinja2 模板 → LLM 生成 SQL → 解析 JSON 提取 SQL
```

模板文件：`resources/conf/prompt/nl2sql_template_jinja2.json`

执行流程（[retrieve.go](../domain/knowledge/service/retrieve.go) `nl2SqlExec`）：

1. `nl2Sql.NL2SQL` — LLM 生成 SQL（传入 ChatHistory + TableSchema）
2. `addSliceIdColumn` — 追加 `_knowledge_slice_id` 选择列
3. `sqlparser.ParseAndModifySQL` — 虚拟表名/列名映射到物理 RDB 表
4. `rdb.ExecuteSQL` — 执行 SQL
5. 遍历结果集行，逐列还原数据类型，序列化为 JSON 作为 `Document.Content`
6. `d.WithScore(1)` — SQL 召回结果分数固定为 1

触发条件：
- 文档类型为 `DocumentTypeTable`
- `Strategy.EnableNL2SQL == true`

### 3.8 重排序（reRankNode）

[retrieve.go](../domain/knowledge/service/retrieve.go) 的 `reRankNode` 按 SearchType 组装通道：

```go
var retrieveResultArr [][]*rerank.Data

if retrieveCtx.Strategy.EnableNL2SQL {
    retrieveResultArr = append(retrieveResultArr, docs2RerankData(nl2SqlRetrieveResult))
}
switch retrieveCtx.Strategy.SearchType {
case SearchTypeSemantic:
    retrieveResultArr = append(retrieveResultArr, docs2RerankData(vectorRetrieveResult))
case SearchTypeFullText:
    retrieveResultArr = append(retrieveResultArr, docs2RerankData(esRetrieveResult))
case SearchTypeHybrid:
    retrieveResultArr = append(retrieveResultArr, docs2RerankData(vectorRetrieveResult))
    retrieveResultArr = append(retrieveResultArr, docs2RerankData(esRetrieveResult))
default:
    retrieveResultArr = append(retrieveResultArr, docs2RerankData(vectorRetrieveResult))
}

resp, err := k.reranker.Rerank(ctx, &rerank.Request{
    Query: query,
    Data:  retrieveResultArr,
    TopN:  retrieveCtx.Strategy.TopK,
})

// MinScore 过滤
for _, item := range resp.SortedData {
    if item.Score < ptr.From(retrieveCtx.Strategy.MinScore) {
        continue
    }
    doc := item.Document
    doc.WithScore(item.Score)
    retrieveResult = append(retrieveResult, doc)
}
```

Reranker 实现选择（[rerank/impl/impl.go](../infra/document/rerank/impl/impl.go)）：
- `RerankType_RRF`（默认）→ 本地 RRF，无网络调用
- `RerankType_VikingDB` → VikingDB 模型重排序服务

RRF 算法详见 [docs/rrf.md](./rrf.md)。

### 3.9 结果打包（packResults）

[retrieve.go](../domain/knowledge/service/retrieve.go) 的 `packResults` 将重排后的 `schema.Document` 转为最终返回的 `RetrieveSlice`：

1. **区分有 ID / 无 ID 结果**：
   - 有 ID：解析 slice ID，收集到 `sliceIDs` set
   - 无 ID（RAGFlow 外部结果）：直接用 MetaData 构造 `RetrieveSlice`，Score=1
2. **批量查询**：`sliceRepo.MGetSlices` 取 slice 记录，`FilterEnableKnowledge` 过滤启用知识库，`MGetByID` 取文档
3. **表格数据填充**：表格类型 slice 调 `selectTableData` 从 RDB 取原始列数据
4. **按文档类型构造内容**：

| 文档类型 | RawContent 填充方式 |
|----------|---------------------|
| Text | `formatSliceContent` 格式化后取 text |
| Table | 优先 sliceMap 原始列数据，否则回退 slice.Content |
| Image | `<img data-tos-key=URI>` + 内容 |

5. **生成文档 URL**：`storage.GetObjectUrl` 生成可访问链接
6. **递增命中计数**：`sliceRepo.IncrementHitCount`（失败仅告警，不中断）

### 3.10 图片内容处理

`formatSliceContent`（retrieve.go L814）：将 slice 内容中的 `<img data-tos-key="...">` 标签替换为真实 TOS URL。

`ParseFrontEndImageContent`（retrieve.go L862）：正则提取图片标签属性，倒序遍历保证偏移量替换不失效。

## 4. RAGFlow 集成

RAGFlow 是外部 RAG 服务，与原始三路召回**互斥**（通过 `ragflowEnabled` 控制，当前硬编码为 `false`）。

### 4.1 初始化

```go
// application/application.go
ragflowBaseURL := os.Getenv("RAGFLOW_BASE_URL")
ragflowAPIKey := os.Getenv("RAGFLOW_API_KEY")
if ragflowBaseURL != "" && ragflowAPIKey != "" {
    ragflowClient = ragflow.NewClient(ragflowBaseURL, ragflowAPIKey)
    knowledgeComponents.RagflowClient = ragflowClient
}
```

### 4.2 检索参数

`ragflowRetrieveNode` 调用 RAGFlow Retrieve API，参数硬编码：

| 参数 | 值 |
|------|----|
| TopK | 策略值，默认 8 |
| SimilarityThreshold | 0.3 |
| VectorSimilarityWeight | 0.7 |
| Page | 1 |
| PageNum | 10 |

### 4.3 客户端能力

[infra/ragflow/client.go](../infra/ragflow/client.go) 封装了完整的 RAGFlow API：Dataset 管理、文档上传/解析、Chunk 管理、检索等。

## 5. 配置结构

### 5.1 KnowledgeConfig

[api/model/admin/config/config.go](../api/model/admin/config/config.go) 定义 Thrift 配置：

```go
type KnowledgeConfig struct {
    EmbeddingConfig *EmbeddingConfig  // 嵌入模型
    RerankConfig    *RerankConfig     // 重排序
    OcrConfig       *OCRConfig        // OCR
    ParserConfig    *ParserConfig     // 解析器
    BuiltinModelID  int64             // 内置模型 ID
}
```

### 5.2 配置加载与回退

[bizpkg/config/knowledge/knowledge.go](../bizpkg/config/knowledge/knowledge.go) 从 KV 存储读取配置，不存在时回退到环境变量：

| 配置项 | 环境变量 |
|--------|----------|
| Embedding 类型 | `EMBEDDING_TYPE` |
| Embedding URL | `{TYPE}_EMBEDDING_BASE_URL` |
| Embedding APIKey | `{TYPE}_EMBEDDING_API_KEY` |
| Embedding 模型 | `{TYPE}_EMBEDDING_MODEL` |
| Embedding 维度 | `{TYPE}_EMBEDDING_DIMS` |
| Rerank 类型 | `RERANK_TYPE` |
| VikingDB AK/SK | `VIKINGDB_RERANK_*` |
| OCR 类型 | `OCR_TYPE` |
| Parser 类型 | `PARSER_TYPE` |

### 5.3 Embedding 支持

支持的 Embedding 类型：`openai` / `ark`（默认）/ `ollama` / `gemini` / `http`

是否启用 sparse 混合检索取决于 Embedding 是否同时支持 dense 和 sparse 向量。

### 5.4 向量存储后端选择

通过 `VECTOR_STORE_TYPE` 环境变量选择：`milvus` / `vikingdb` / `oceanbase`

### 5.5 消息队列选择

通过 `MQTypeKey` 环境变量选择：`nsq` / `kafka` / `rmq` / `pulsar` / `nats`

## 6. 关键设计决策

### 6.1 事件驱动的异步索引

索引采用 MQ 异步处理，API 请求只负责上传文件和发事件，不阻塞等待索引完成。两级事件机制（批量 → 单文档）保证同一知识库的事件顺序性。

### 6.2 eino 框架编排

检索链路使用 eino 的 `compose.Chain` + `compose.Parallel` 编排 DAG，各节点解耦可独立测试，并行召回自动并发执行。

### 6.3 多路召回 + RRF 融合

默认三路召回（向量 + ES + NL2SQL）各自独立返回结果，通过 RRF 按排名融合。RRF 不依赖各系统分数的绝对值，只看相对排名，适合融合异构检索系统。

### 6.4 表格类型特殊处理

表格文档有独立的处理路径：
- 索引时：创建物理 RDB 表，列数据可压缩（`enableCompactTable`）
- 检索时：支持 NL2SQL 直接查 RDB，或通过 `MultiMatch` 多列匹配
- 打包时：从 RDB 取原始列数据填充 slice

### 6.5 RAGFlow 互斥模式

RAGFlow 作为外部 RAG 服务，启用时完全替换原始三路召回，不做混合。当前 `ragflowEnabled` 硬编码为 `false`。

## 7. 关键文件索引

### 索引链路

| 职责 | 文件 |
|------|------|
| API 入口 | [application/knowledge/knowledge.go](../application/knowledge/knowledge.go) |
| 初始化装配 | [application/knowledge/init.go](../application/knowledge/init.go) |
| CreateDocument | [domain/knowledge/service/knowledge.go](../domain/knowledge/service/knowledge.go) |
| MQ 事件处理 | [domain/knowledge/service/event_handle.go](../domain/knowledge/service/event_handle.go) |
| DocProcessor 接口 | [domain/knowledge/processor/interface.go](../domain/knowledge/processor/interface.go) |
| DocProcessor 实现 | [domain/knowledge/processor/impl/base.go](../domain/knowledge/processor/impl/base.go) |
| 事件定义 | [domain/knowledge/entity/event.go](../domain/knowledge/entity/event.go) |
| 事件工厂 | [domain/knowledge/internal/events/events.go](../domain/knowledge/internal/events/events.go) |
| 文档状态 | [domain/knowledge/entity/const.go](../domain/knowledge/entity/const.go) |
| 分片策略 | [domain/knowledge/entity/strategy.go](../domain/knowledge/entity/strategy.go) |
| 默认参数 | [domain/knowledge/internal/consts/consts.go](../domain/knowledge/internal/consts/consts.go) |

### 检索链路

| 职责 | 文件 |
|------|------|
| 检索编排 | [domain/knowledge/service/retrieve.go](../domain/knowledge/service/retrieve.go) |
| 服务接口 | [domain/knowledge/service/interface.go](../domain/knowledge/service/interface.go) |
| 类型转换 | [domain/knowledge/service/convert.go](../domain/knowledge/service/convert.go) |
| 可写性校验 | [domain/knowledge/service/validate.go](../domain/knowledge/service/validate.go) |
| 检索模型 | [crossdomain/knowledge/model/knowledge.go](../crossdomain/knowledge/model/knowledge.go) |

### Infra 层

| 职责 | 文件 |
|------|------|
| Parser 接口 | [infra/document/parser/manager.go](../infra/document/parser/manager.go) |
| Parser builtin | [infra/document/parser/impl/builtin/](../infra/document/parser/impl/builtin/) |
| Parser ppstructure | [infra/document/parser/impl/ppstructure/](../infra/document/parser/impl/ppstructure/) |
| SearchStore 接口 | [infra/document/searchstore/searchstore.go](../infra/document/searchstore/searchstore.go) |
| Manager 接口 | [infra/document/searchstore/manager.go](../infra/document/searchstore/manager.go) |
| DSL 定义 | [infra/document/searchstore/dsl.go](../infra/document/searchstore/dsl.go) |
| Manager 工厂 | [infra/document/searchstore/impl/impl.go](../infra/document/searchstore/impl/impl.go) |
| Milvus 实现 | [infra/document/searchstore/impl/milvus/](../infra/document/searchstore/impl/milvus/) |
| ES 实现 | [infra/document/searchstore/impl/elasticsearch/](../infra/document/searchstore/impl/elasticsearch/) |
| Reranker 接口 | [infra/document/rerank/rerank.go](../infra/document/rerank/rerank.go) |
| RRF 实现 | [infra/document/rerank/impl/rrf/rrf.go](../infra/document/rerank/impl/rrf/rrf.go) |
| Reranker 工厂 | [infra/document/rerank/impl/impl.go](../infra/document/rerank/impl/impl.go) |
| NL2SQL | [infra/document/nl2sql/](../infra/document/nl2sql/) |
| MessagesToQuery | [infra/document/messages2query/](../infra/document/messages2query/) |
| EventBus | [infra/eventbus/](../infra/eventbus/) |
| RAGFlow | [infra/ragflow/client.go](../infra/ragflow/client.go) |

### 配置层

| 职责 | 文件 |
|------|------|
| Thrift 配置 | [api/model/admin/config/config.go](../api/model/admin/config/config.go) |
| 配置加载 | [bizpkg/config/knowledge/knowledge.go](../bizpkg/config/knowledge/knowledge.go) |

## 8. 小结

| 维度 | 实现 |
|------|------|
| 索引方式 | 事件驱动异步索引（MQ），两级事件保证顺序 |
| 文档解析 | 支持 PDF/TXT/MD/DOCX/CSV/XLSX/JSON/图片，builtin + ppstructure |
| 分片策略 | Default/Custom/Leveled，默认 ChunkSize=800, Separator=\n, Overlap=10% |
| 向量存储 | Milvus（默认）/ VikingDB / OceanBase，支持 dense+sparse 混合 |
| 全文检索 | Elasticsearch |
| 检索编排 | eino compose.Chain DAG，查询改写 → 并行多路召回 → 重排 → 打包 |
| 多路召回 | 向量 + ES + NL2SQL（表格），按 SearchType 路由 |
| 重排序 | RRF（默认，本地）/ VikingDB（模型服务），详见 rrf.md |
| Query 改写 | LLM 基于对话历史改写，Jinja2 模板 |
| NL2SQL | LLM 生成 SQL 查 RDB，表格类型专属 |
| 外部集成 | RAGFlow（互斥模式，当前未启用） |
| 消息队列 | nsq / kafka / rmq / pulsar / nats |
| 并发控制 | 召回 errgroup limit=2，批量索引 batchSize=100 |
