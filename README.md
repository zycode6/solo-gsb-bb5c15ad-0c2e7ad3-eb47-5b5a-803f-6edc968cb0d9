# 食品工坊配方标签核查 API

供小型食品工坊使用的配方标签核对服务：登记原料及其过敏原，用**带独立条目编号**的原料或子配方组成配方；保存配方时校验引用与循环；查询时展开全部引用路径并按过敏原汇总每条来源路径，用于成品标签核对。改动配方前还可用**标签预演**接口，在不写入数据库的前提下预演整体替换后的过敏原变化，判断是否需要重印标签。

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
| POST | `/recipes/{id}/label-preview` | **标签预演**：接收与整体替换相同的请求，校验相同但**不写入数据库**，返回替换前后的过敏原变化 |
| GET | `/recipes` / `/recipes/{id}` | 查询配方及其条目 |
| GET | `/recipes/{id}/expansion` | **标签核对**：展开全部引用路径，按过敏原汇总来源 |

## 保存规则（重点）

保存配方（POST / PUT）时：

1. **引用必须存在**：条目引用了不存在的原料或子配方 → `400 UNKNOWN_REFERENCE`，返回全部缺失引用；
2. **拒绝循环**：新组成会导致配方直接或间接引用回自身 → `409 CYCLE_DETECTED`，返回循环路径；
3. **失败零改动**：所有校验通过后才写入，删除旧条目与写入新条目在同一事务提交——任何校验失败都不会改动数据库中的原配方。

`POST /recipes/{id}/label-preview` 沿用完全相同的校验（引用存在性、循环、名称冲突），校验失败时返回与保存接口一致的错误；校验通过时只做展开与对比，**不写入任何数据**。

## 标签预演（判断是否重印标签）

`POST /recipes/{id}/label-preview` 接收与整体替换（PUT）相同的请求体，分别展开**当前配方**与**预演配方**的全部原料路径，按过敏原对比来源后返回三组结果：

| 字段 | 含义 |
|---|---|
| `added_allergens` | 替换后新增的过敏原（`before` 为空） |
| `removed_allergens` | 替换后移除的过敏原（`after` 为空） |
| `changed_allergens` | 仍存在、但来源原料或路径发生变化的过敏原 |

每项都列出变更前后的来源原料与路径（`before` / `after`），便于判断是否需要重印标签。路径标识由**每层条目位置**依次组成（如 `[2, 1]` 表示「本配方第 2 行 → 子配方第 1 行」）；同一子配方被重复引用时，每次引用位置不同，各自保留为不同来源。

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

# 5. 标签预演：打算把曲奇改成「黄油霜×1 + 花生酱」，先看标签变化（不写入）
curl -X POST $B/ingredients -H 'Content-Type: application/json' \
  -d '{"name":"花生酱","allergens":["花生"]}'        # -> ING-0004
curl -X POST $B/recipes/REC-0002/label-preview -H 'Content-Type: application/json' -d '{
  "name":"曲奇",
  "entries":[
    {"ref_type":"recipe","ref_id":"REC-0001","quantity":1,"unit":"份"},
    {"ref_type":"ingredient","ref_id":"ING-0004","quantity":50,"unit":"g"}
  ]}'
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

第 5 步的预演响应如下（数据库中的曲奇配方保持不变）：

```json
{
  "recipe_id": "REC-0002",
  "recipe_name": "曲奇",
  "preview_name": "曲奇",
  "added_allergens": [
    {"allergen": "花生", "before": [], "after": [
      {"ingredient_id": "ING-0004", "ingredient_name": "花生酱", "path": [2]}
    ]}
  ],
  "removed_allergens": [
    {"allergen": "麸质", "before": [
      {"ingredient_id": "ING-0002", "ingredient_name": "面粉", "path": [1]}
    ], "after": []}
  ],
  "changed_allergens": [
    {"allergen": "乳",
     "before": [
       {"ingredient_id": "ING-0001", "ingredient_name": "黄油", "path": [2, 1]},
       {"ingredient_id": "ING-0001", "ingredient_name": "黄油", "path": [3, 1]}
     ],
     "after": [
       {"ingredient_id": "ING-0001", "ingredient_name": "黄油", "path": [1, 1]}
     ]}
  ]
}
```

即：新增「花生」、移除「麸质」；「乳」仍在，但来源从两条路径（黄油霜被引用两次）变为一条——标签需要重印。确认无误后再用 `PUT /recipes/REC-0002` 正式保存同样的请求体。

## 项目结构

```
app/
├── main.py            # 应用入口，启动时初始化数据库
├── database.py        # 引擎 / 会话 / 建表（DATABASE_URL 可配置）
├── models.py          # Ingredient / Recipe / RecipeEntry / 发号器
├── schemas.py         # 请求与响应模型
├── services.py        # 校验（引用、循环）、原子保存、展开汇总、标签预演对比
└── routers/           # /ingredients、/recipes 路由
tests/test_api.py      # 18 个行为测试（校验、循环、原子性、展开、标签预演）
Dockerfile             # python:3.12-slim，构建时安装依赖
docker-compose.yml     # 一条命令启动，端口与数据卷在此配置
```
