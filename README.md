# 食品工坊配方标签核查 API

供小型食品工坊使用的配方标签核对服务：登记原料及其过敏原，用**带独立条目编号**的原料或子配方组成配方；保存配方时校验引用与循环；查询时展开全部引用路径并按过敏原汇总每条来源路径，用于成品标签核对。

技术栈：Python 3.12 · FastAPI · SQLAlchemy 2 · SQLite

## 一条命令启动

```bash
docker compose up --build
```

依赖安装随镜像构建完成，SQLite 数据库（建目录、建表）随应用启动自动初始化，无需任何手工步骤。

启动后访问：

| 地址 | 说明 |
|---|---|
| http://localhost:8000 | 服务信息 |
| http://localhost:8000/docs | Swagger 交互式文档（可直接试调用） |
| http://localhost:8000/health | 健康检查 |

## 配置

| 配置项 | 默认值 | 说明 |
|---|---|---|
| 访问端口 | `8000` | 改 `docker-compose.yml` 中 `ports: ["8080:8000"]` 的**前一个**端口即可换访问端口 |
| `DATABASE_URL` | 容器内 `sqlite:////data/labels.db`；本地运行 `sqlite:///./data/labels.db` | SQLAlchemy 连接串，SQLite 文件目录会自动创建 |
| 数据持久化 | named volume `label-data` → `/data` | 删除容器不丢数据 |

本地开发（不用 Docker）：

```bash
pip install -r requirements-dev.txt
uvicorn app.main:app --reload --port 8000   # 启动时自动初始化 ./data/labels.db
pytest                                       # 运行测试
```

## 编号规则

| 编号 | 格式 | 说明 |
|---|---|---|
| 原料 | `ING-0001` | 登记时自动分配 |
| 配方 | `REC-0001` | 创建时自动分配 |
| 条目 | `ENT-000001` | 配方中每一行组成的**独立条目编号**，全局唯一；同一子配方被使用多次时，每次使用各有自己的条目编号 |

## 接口一览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/ingredients` | 登记原料及过敏原（过敏原自动去重去空白） |
| GET | `/ingredients` / `/ingredients/{id}` | 查询原料 |
| POST | `/recipes` | 新建配方（条目按顺序组成，可引用原料或子配方） |
| PUT | `/recipes/{id}` | 整体替换配方组成 |
| POST | `/recipes/{id}/label-preview` | **标签预演**：按整体替换的请求体预演标签变化，**不写入数据库** |
| GET | `/recipes` / `/recipes/{id}` | 查询配方及其条目 |
| GET | `/recipes/{id}/expansion` | **标签核对**：展开全部引用路径，按过敏原汇总来源 |

## 保存规则（重点）

保存配方（POST / PUT）时：

1. **引用必须存在**：条目引用了不存在的原料或子配方 → `400 UNKNOWN_REFERENCE`，返回全部缺失引用；
2. **拒绝循环**：新组成会导致配方直接或间接引用回自身 → `409 CYCLE_DETECTED`，返回循环路径；
3. **失败零改动**：所有校验通过后才写入，删除旧条目与写入新条目在同一事务提交——任何校验失败都不会改动数据库中的原配方。

## 使用示例

```bash
B=http://localhost:8000

# 1. 登记原料
curl -X POST $B/ingredients -H 'Content-Type: application/json' \
  -d '{"name":"黄油","allergens":["乳"]}'          # -> ING-0001
curl -X POST $B/ingredients -H 'Content-Type: application/json' \
  -d '{"name":"面粉","allergens":["麸质"]}'        # -> ING-0002
curl -X POST $B/ingredients -H 'Content-Type: application/json' \
  -d '{"name":"砂糖","allergens":[]}'              # -> ING-0003

# 2. 子配方：黄油霜
curl -X POST $B/recipes -H 'Content-Type: application/json' -d '{
  "name":"黄油霜",
  "entries":[
    {"ref_type":"ingredient","ref_id":"ING-0001","quantity":250,"unit":"g"},
    {"ref_type":"ingredient","ref_id":"ING-0003","quantity":100,"unit":"g"}
  ]}'                                            # -> REC-0001（条目 ENT-000001/2）

# 3. 主配方：曲奇（同一子配方使用两次，各自保留条目编号）
curl -X POST $B/recipes -H 'Content-Type: application/json' -d '{
  "name":"曲奇",
  "entries":[
    {"ref_type":"ingredient","ref_id":"ING-0002","quantity":300,"unit":"g"},
    {"ref_type":"recipe","ref_id":"REC-0001","quantity":1,"unit":"份"},
    {"ref_type":"recipe","ref_id":"REC-0001","quantity":1,"unit":"份"}
  ]}'                                            # -> REC-0002（条目 ENT-000003/4/5）

# 4. 标签核对：展开全部路径 + 过敏原来源汇总
curl $B/recipes/REC-0002/expansion
```

展开结果中，每条来源路径由条目编号依次记录（如 `["ENT-000004","ENT-000001"]` 表示「曲奇第 2 行 → 黄油霜第 1 行 → 黄油」）；`allergen_summary` 按过敏原分组，同一子配方使用两次会列出两条独立来源路径：

```json
"allergen_summary": [
  {"allergen": "乳", "sources": [
    {"path": ["ENT-000004", "ENT-000001"], "ingredient_id": "ING-0001", "ingredient_name": "黄油"},
    {"path": ["ENT-000005", "ENT-000001"], "ingredient_id": "ING-0001", "ingredient_name": "黄油"}
  ]},
  {"allergen": "麸质", "sources": [
    {"path": ["ENT-000003"], "ingredient_id": "ING-0002", "ingredient_name": "面粉"}
  ]}
]
```

## 标签预演：不落库先看标签要不要重印

`POST /recipes/{id}/label-preview` 的请求体与 `PUT /recipes/{id}` **完全相同**，但只做预演、绝不写入数据库：

- **同样的校验**：引用存在性（`400 UNKNOWN_REFERENCE`）、循环引用（`409 CYCLE_DETECTED`）、名称冲突（`409 DUPLICATE_NAME`）、配方不存在（`404 RECIPE_NOT_FOUND`），错误结构与保存接口一致；
- **同样的展开**：分别展开「当前配方」与「预演配方」的全部原料路径。路径以**每层条目位置**（行号，从 1 开始）组成标识，例如 `[2, 1]` 表示「根配方第 2 行 → 子配方第 1 行」；同一子配方引用多次时行号不同，重复引用会作为不同来源分别保留；
- **差异结果**：
  - `added_allergens` / `removed_allergens`：预演后新增 / 消失的过敏原，分别列出预演侧 / 当前侧的全部来源原料与路径；
  - `changed_allergens`：过敏原仍存在，但来源原料或路径发生了变化，逐项给出变更前后来源：`removed_sources`（原来源）、`added_sources`（变化后来源）、`unchanged_sources`（位置与原料均未变的来源，数量 / 单位变化不视为变化）；
  - `allergen_list_changed=true`：过敏原名单本身增减，标签文字必然要改，`reprint_recommended=true`；
  - 仅 `sources_changed=true`：名单未变但溯源来源变了，文字可保持原样，是否重印由工坊按溯源要求自行判断。

以上一节的曲奇为例（`REC-0002`：面粉 + 2 次黄油霜），预演「去掉面粉、两个子配方引用保留」：

```bash
B=http://localhost:8000
curl -X POST $B/recipes/REC-0002/label-preview \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "曲奇",
    "entries": [
      {"ref_type": "recipe", "ref_id": "REC-0001", "quantity": 1, "unit": "份"},
      {"ref_type": "recipe", "ref_id": "REC-0001", "quantity": 1, "unit": "份"}
    ]
  }'
```

返回（已省略 `current` / `preview` 两侧完整展开，结构同 `/expansion`，仅路径标识改用每层行号）：

```json
{
  "recipe_id": "REC-0002",
  "current": { "recipe_name": "曲奇", "leaf_count": 5, "paths": ["..."], "allergen_summary": ["..."] },
  "preview": { "recipe_name": "曲奇", "leaf_count": 4, "paths": ["..."], "allergen_summary": ["..."] },
  "added_allergens": [],
  "removed_allergens": [
    {"allergen": "麸质", "before_sources": [
      {"path": [1], "steps": [{"position": 1, "ref_id": "ING-0002", "ref_name": "面粉"}],
       "ingredient_id": "ING-0002", "ingredient_name": "面粉"}]}
  ],
  "changed_allergens": [],
  "allergen_list_changed": true,
  "sources_changed": false,
  "reprint_recommended": true
}
```

预演不改变数据库：调用后再 `GET $B/recipes/REC-0002`，名称与组成仍为预演前的内容；确认无误后，再用同样的请求体调 `PUT $B/recipes/REC-0002` 正式保存即可。

## 项目结构

```
app/
├── main.py            # 应用入口，启动时初始化数据库
├── database.py        # 引擎 / 会话 / 建表（DATABASE_URL 可配置）
├── models.py          # Ingredient / Recipe / RecipeEntry / 发号器
├── schemas.py         # 请求与响应模型（含标签预演差异模型）
├── services.py        # 校验（引用、循环、名称）、原子保存、展开汇总、标签预演与过敏原差异
└── routers/           # /ingredients、/recipes 路由
tests/test_api.py      # 21 个行为测试（校验、循环、原子性、展开、标签预演）
Dockerfile             # python:3.12-slim，构建时安装依赖
docker-compose.yml     # 一条命令启动，端口与数据卷在此配置
```
