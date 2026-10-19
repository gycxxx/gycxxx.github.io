---
title: "RRF（Reciprocal Rank Fusion，倒数排名融合）说明"
date: 2026-09-30
categories:
  - "RAG"
tags:
  - "RRF"
  - "检索"
---

本文档整理 OpenCoze 中 RRF 重排序机制的位置、实现与算法细节。

## 1. 概述

RRF 是一种多路检索结果融合算法：不依赖各检索系统相似度分数的绝对值，只根据文档在每个结果列表中的**排名**计算融合得分，从而把向量检索、全文检索等异构召回结果统一排序。

核心公式：

```
score(d) = 1 / (rank(d) + k)
```

其中 `rank` 为文档在某个结果列表中的名次（0-based），`k` 为平滑常数（本项目默认 60，RRF 原始论文 Cormack et al. 2009 的推荐值，与 Elasticsearch、Milvus 的默认值一致）。

## 2. 在检索链路中的位置

RRF 位于知识库检索（RAG）链路的**重排序阶段**，完整链路如下：

```
用户 Query
   │
   ├─ (可选) Query Rewrite ── 改写查询
   │
   ├─ 多路召回（并行）
   │    ├─ 向量检索（Milvus，语义检索）   ──► 结果列表 A
   │    ├─ 全文检索（Elasticsearch）      ──► 结果列表 B
   │    └─ (可选) NL2SQL 检索            ──► 结果列表 C
   │
   ├─ RRF 重排序（本文档）
   │    输入：[[列表A], [列表B], [列表C]]
   │    输出：去重后按融合分数降序的单一列表
   │
   └─ MinScore 过滤 + TopK 截断 ──► 最终检索结果
```

关键代码位于 [retrieve.go](../domain/knowledge/service/retrieve.go)（`rerankKnowledge` 相关逻辑）：

- 检索策略 `SearchTypeHybrid` 时，向量结果与 ES 结果**同时**进入融合列表
- `SearchTypeSemantic` / `SearchTypeFullText` 时只有单路结果（此时 RRF 退化为按原排名去重排序）
- 启用 `EnableNL2SQL` 时，NL2SQL 结果也作为一个独立列表参与融合
- Rerank 返回后，低于 `Strategy.MinScore` 的文档被过滤，最终受 `Strategy.TopK` 限制

## 3. 代码位置

| 职责 | 文件 |
|------|------|
| Reranker 接口定义 | `infra/document/rerank/rerank.go` |
| RRF 核心实现 | `infra/document/rerank/impl/rrf/rrf.go` |
| Reranker 工厂（按配置选择实现） | `infra/document/rerank/impl/impl.go` |
| 检索链路调用点 | `domain/knowledge/service/retrieve.go` |
| Milvus 服务端 RRF（另一处，见 §7） | `infra/document/searchstore/impl/milvus/milvus_searchstore.go` |

## 4. 接口定义

```go
// infra/document/rerank/rerank.go
type Reranker interface {
    Rerank(ctx context.Context, req *Request) (*Response, error)
}

type Request struct {
    Query string      // 原始或改写后的查询
    Data  [][]*Data   // 二维结构：多路召回的多个结果列表
    TopN  *int64      // 最终保留条数（来自 Strategy.TopK）
}

type Response struct {
    SortedData []*Data // 按分数降序
    TokenUsage *int64
}

type Data struct {
    Document *schema.Document
    Score    float64
}
```

## 5. 核心实现

```go
// infra/document/rerank/impl/rrf/rrf.go
func NewRRFReranker(k int64) rerank.Reranker {
    if k == 0 {
        k = 60 // 传 0 即使用论文推荐默认值
    }
    return &rrfReranker{k}
}

func (r *rrfReranker) Rerank(ctx context.Context, req *rerank.Request) (*rerank.Response, error) {
    if req == nil || req.Data == nil || len(req.Data) == 0 {
        return nil, fmt.Errorf("invalid request: no data provided")
    }
    id2Score := make(map[string]float64)
    id2Data := make(map[string]*rerank.Data)
    for _, resultList := range req.Data {          // 遍历每一路召回列表
        for rank := range resultList {              // rank 为 0-based 名次
            result := resultList[rank]
            if result != nil && result.Document != nil {
                score := 1.0 / (float64(rank) + float64(r.k))  // RRF 公式
                if score > id2Score[result.Document.ID] {      // 同 ID 只保留最高分
                    id2Score[result.Document.ID] = score
                    id2Data[result.Document.ID] = result
                }
            }
        }
    }
    var sorted []*rerank.Data
    for _, data := range id2Data {                  // map 去重后收集
        sorted = append(sorted, data)
    }
    sort.Slice(sorted, func(i, j int) bool {        // 按分数降序
        return id2Score[sorted[i].Document.ID] > id2Score[sorted[j].Document.ID]
    })
    topN := int64(len(sorted))
    if req.TopN != nil && ptr.From(req.TopN) != 0 && ptr.From(req.TopN) < topN {
        topN = ptr.From(req.TopN)                   // TopN 截断
    }
    return &rerank.Response{SortedData: sorted[:topN]}, nil
}
```

执行步骤：

1. **校验**：空请求直接报错
2. **打分**：对每路列表中每个文档按 `1/(rank+k)` 计算分数
3. **去重**：同一 `Document.ID` 出现在多个列表时，仅保留其最高得分（见 §6 与标准算法的差异）
4. **排序**：按融合分数从高到低
5. **截断**：`TopN` 有效且小于总数时，只返回前 N 条

## 6. 与标准 RRF 算法的差异（重要）

标准 RRF（论文及 Elasticsearch 实现）对同一文档在**多个列表中的得分求和**：

```
RRF(d) = Σ_i  1 / (rank_i(d) + k)      // 标准算法：跨列表累加
```

本实现改为**取最大值**：

```
score(d) = max_i  1 / (rank_i(d) + k)  // 本实现：只取最好名次
```

影响：

- **标准算法（求和）**：文档在多路召回中"都出现"会持续加分，鼓励多路一致性——一个文档同时被向量检索和全文检索召回时显著受益
- **本实现（取最大）**：只看文档在所有列表中的**最好名次**，多路同时命中不再有叠加收益，效果上更接近"按各路最优名次统一去重排序"

如需对齐标准 RRF 行为，可将 `if score > id2Score[...]` 的分支改为累加：

```go
id2Score[result.Document.ID] += score
id2Data[result.Document.ID] = result
```

## 7. k 的取值逻辑

### 当前取值：固定 60

`k` 虽是构造参数，但当前**全项目所有调用点均传 0**，经 `NewRRFReranker` 内部兜底后等效于硬编码 60：

```go
// infra/document/rerank/impl/impl.go —— RRF 与 default 分支均传 0
func New(conf *config.KnowledgeConfig) Reranker {
    switch conf.RerankConfig.Type {
    case config.RerankType_VikingDB:
        return vikingdb.NewReranker(conf.RerankConfig.VikingdbConfig)
    case config.RerankType_RRF:
        return rrf.NewRRFReranker(0)
    default:
        return rrf.NewRRFReranker(0)
    }
}
```

工厂逻辑说明：知识库未配置 Rerank 或配置为 RRF 类型时，均使用本地 RRF（无网络调用、零成本）；配置为 VikingDB 时使用其模型重排序服务。

### k 的数学作用：控制名次间分数的衰减曲线

| rank | k=1 | k=60 | k=1000 |
|------|------|------|--------|
| 0 | 1.000 | 0.01667 | 0.001000 |
| 1 | 0.500 | 0.01639 | 0.000999 |
| 10 | 0.091 | 0.01429 | 0.000990 |
| 59 | 0.017 | 0.00840 | 0.000944 |

- **k 小**（如 k=1）：头名与次名分数差一倍，单路冠军几乎垄断融合结果
- **k=60**（默认）：名次间差距被压平（rank 0 与 rank 1 仅差约 0.0003），更看重文档的整体排名稳定性，对单路噪声更鲁棒
- **k 极大**：所有名次分数趋近相等，退化为近似"按出现情况排序"

### 如何自定义 k

在 `impl.go` 调用处传入非零值，或从 `KnowledgeConfig` 透传配置：

```go
case config.RerankType_RRF:
    return rrf.NewRRFReranker(conf.RerankConfig.RRFK) // 需扩展配置结构
```

## 8. 另一处 RRF：Milvus 服务端混合检索

除应用层 RRF 外，Milvus 向量库内部还有一处服务端 RRF（`milvus_searchstore.go` 的 `HybridSearch`）：

```go
searchOption := client.NewHybridSearchOption(m.collectionName, ptr.From(options.TopK), annRequests...).
    WithReranker(client.NewRRFReranker()). // Milvus SDK 内置，由 Milvus 服务端执行
    ...
```

这是对**同一 collection 内多个向量索引**（如多 embedding 模型的多个向量字段）做服务端融合，与应用层的多路（Milvus+ES+NL2SQL）融合是两层不同的 RRF，互不影响。

## 9. 小结

| 项 | 值 |
|----|----|
| 算法 | RRF 倒数排名融合（取最大值变体，见 §6） |
| k 默认值 | 60（论文推荐，当前不可配置） |
| 触发条件 | 知识库未配置 Reranker，或配置为 RRF 类型 |
| 输入 | 多路召回结果列表（向量 / ES / NL2SQL） |
| 输出 | 去重、按融合分数降序、TopK 截断后的列表 |
| 后处理 | `MinScore` 过滤（retrieve.go） |
| 成本 | 纯本地计算，无网络调用、无模型推理开销 |
