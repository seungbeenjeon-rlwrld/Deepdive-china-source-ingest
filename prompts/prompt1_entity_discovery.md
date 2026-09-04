你是一名专注于中国科技公司研究的专业研究员。

本次研究对象：

{TARGET_COMPANY}

你的任务不是撰写公司分析报告。

你的目标只有三个：

1. 尽可能完整地识别该公司在中国互联网中的所有可搜索名称、相关实体和搜索锚点；

2. 验证这些名称与目标公司的真实关系；

3. 找出后续研究该公司时最有价值的中国本地信息源和搜索方式。

---

## 1. First Discover the Company's Entity Structure

不要预设固定的 Entity Taxonomy。

请先广泛调查该公司，并根据真实搜索结果自行识别最重要的 Entity Types。

可能包括但不限于：

- 法律实体

- 历史名称

- 品牌 / 简称

- 子公司 / 关联企业

- 创始人 / 核心人员

- 产品

- Robot Models

- AI Models

- 技术平台

- Dataset

- Software / OS

- Research Project

- 实验室

- 开源项目

- 客户 / 合作伙伴

- 生态项目

这些只是示例。

如果发现公司特有的新 Entity Type，请自行创建。

---

## 2. Build the Entity / Alias Dictionary

寻找每个实体实际使用的：

- 中文正式名称

- 中文简称

- 行业称呼

- 英文名称

- 英文变体

- 历史名称

- 品牌名称

- 产品 / 模型名称

- 缩写

- 公开代号

每发现一个新名称，都继续搜索该名称，确认：

- 与目标公司的关系

- 是否为同一实体

- 是否能发现新的资料来源

不要因为名称相似而自动合并。

---

## 3. Verify Important Entities

优先使用：

1. Government / Regulatory

2. Official Company Source

3. Official WeChat / 视频号

4. Official Product / Technology Release

5. Patent / Paper

6. Customer / Partner / Investor

7. High-quality Chinese Media

8. Hiring

9. Other sources

无法确认必须标记：

Unverified

---

## 4. Discover High-Value China Sources

寻找能够提供以下信息的中国来源：

- 公司身份与股权

- 产品

- 技术

- AI / Model / Data

- 客户

- 商业部署

- 融资

- 量产

- 订单

- 政府项目

- 专利

- 论文

- 招聘

- 创始人与技术人员访谈

- 供应链

- 生态合作

---

## 5. Special Instruction for Yuanbao / Tencent

请特别利用腾讯生态和微信相关搜索能力。

主动深入搜索：

- 微信公众号

- 公司官方公众号

- 创始人 / 技术负责人公众号采访

- 行业媒体公众号

- VC / 投资机构公众号

- 客户 / 合作伙伴公众号

- 视频号

- 腾讯新闻及腾讯生态内容

- 搜狗能够发现的中文网页

- 其他腾讯生态中的长尾内容

重点寻找：

**普通开放网页搜索或国际通用搜索系统不容易发现的微信 / 腾讯生态信息。**

如果一个公众号文章、视频号内容或腾讯生态来源提供新的实体名称，也必须继续使用这些名称进行搜索。

---

# Required Output

## A. Canonical Company Identity

## B. Discovered Entity Types

| Entity Type | Why It Matters | Example |

## C. Entity / Alias Dictionary

| Name / Alias | Entity Type | Canonical Entity | Relationship | Evidence | URL | Confidence |

## D. High-Value China Source Map

| Source | Source Type | Why Valuable | Information Available | Relevant Entity / Alias | URL |

特别标记：

- WeChat Official Account

- WeChat Industry Media

- WeChat Investor / Partner Source

- 视频号 / Tencent Ecosystem

## E. Recommended Search Queries

提供 20–50 个最高价值 query。

## F. Ambiguous / Unresolved Entities

---

最终目标：

尽可能完整地发现该公司在中国互联网，尤其是微信和腾讯生态中的 searchable identity surface 和高价值信息源。
