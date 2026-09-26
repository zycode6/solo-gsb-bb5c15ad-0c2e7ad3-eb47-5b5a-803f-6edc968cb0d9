"""接口行为测试：登记、校验、循环拒绝、原子保存、展开汇总。"""


def make_ingredient(client, name, allergens=None):
    resp = client.post("/ingredients", json={"name": name, "allergens": allergens or []})
    assert resp.status_code == 201, resp.text
    return resp.json()


def make_recipe(client, name, entries, description=""):
    resp = client.post("/recipes", json={
        "name": name, "description": description, "entries": entries,
    })
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------- 原料 ----------

def test_create_and_list_ingredients(client):
    ing = make_ingredient(client, "黄油", ["乳", "乳"])  # 重复过敏原应去重
    assert ing["id"] == "ING-0001"
    assert ing["allergens"] == ["乳"]

    make_ingredient(client, "低筋面粉", ["麸质"])
    listed = client.get("/ingredients").json()
    assert [i["id"] for i in listed] == ["ING-0001", "ING-0002"]


def test_duplicate_ingredient_name_rejected(client):
    make_ingredient(client, "黄油")
    resp = client.post("/ingredients", json={"name": "黄油", "allergens": []})
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "DUPLICATE_NAME"


# ---------- 配方保存校验 ----------

def test_unknown_reference_rejected_and_nothing_saved(client):
    resp = client.post("/recipes", json={
        "name": "幽灵配方",
        "entries": [
            {"ref_type": "ingredient", "ref_id": "ING-9999"},
            {"ref_type": "recipe", "ref_id": "REC-9999"},
        ],
    })
    assert resp.status_code == 400
    body = resp.json()["detail"]
    assert body["code"] == "UNKNOWN_REFERENCE"
    assert len(body["missing"]) == 2
    # 失败不应产生任何配方
    assert client.get("/recipes").json() == []


def test_direct_self_cycle_rejected(client):
    butter = make_ingredient(client, "黄油", ["乳"])
    sub = make_recipe(client, "基底", [
        {"ref_type": "ingredient", "ref_id": butter["id"]},
    ])
    resp = client.put(f"/recipes/{sub['id']}", json={
        "name": "基底",
        "entries": [{"ref_type": "recipe", "ref_id": sub["id"]}],  # 直接引用自己
    })
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "CYCLE_DETECTED"


def test_indirect_cycle_rejected_and_original_kept(client):
    milk = make_ingredient(client, "牛奶", ["乳"])
    a = make_recipe(client, "配方A", [{"ref_type": "ingredient", "ref_id": milk["id"]}])
    b = make_recipe(client, "配方B", [{"ref_type": "recipe", "ref_id": a["id"]}])
    # 让 A 引用 B，形成 A -> B -> A 的间接循环
    resp = client.put(f"/recipes/{a['id']}", json={
        "name": "配方A",
        "entries": [{"ref_type": "recipe", "ref_id": b["id"]}],
    })
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "CYCLE_DETECTED"

    # 失败时原配方保持不变
    after = client.get(f"/recipes/{a['id']}").json()
    assert len(after["entries"]) == 1
    assert after["entries"][0]["ref_type"] == "ingredient"
    assert after["entries"][0]["ref_id"] == milk["id"]


def test_failed_update_with_unknown_ref_keeps_original(client):
    flour = make_ingredient(client, "面粉", ["麸质"])
    recipe = make_recipe(client, "面团", [
        {"ref_type": "ingredient", "ref_id": flour["id"], "quantity": 500, "unit": "g"},
    ])
    resp = client.put(f"/recipes/{recipe['id']}", json={
        "name": "面团",
        "entries": [{"ref_type": "ingredient", "ref_id": "ING-4242"}],
    })
    assert resp.status_code == 400
    after = client.get(f"/recipes/{recipe['id']}").json()
    assert after["entries"][0]["ref_id"] == flour["id"]
    assert after["entries"][0]["quantity"] == 500


def test_update_replaces_entries_atomically(client):
    flour = make_ingredient(client, "面粉", ["麸质"])
    sugar = make_ingredient(client, "砂糖")
    recipe = make_recipe(client, "面团", [
        {"ref_type": "ingredient", "ref_id": flour["id"]},
    ])
    old_entry_id = recipe["entries"][0]["id"]

    resp = client.put(f"/recipes/{recipe['id']}", json={
        "name": "甜面团",
        "entries": [
            {"ref_type": "ingredient", "ref_id": flour["id"]},
            {"ref_type": "ingredient", "ref_id": sugar["id"]},
        ],
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["name"] == "甜面团"
    assert len(body["entries"]) == 2
    # 新条目使用新的独立条目编号
    assert all(e["id"] != old_entry_id for e in body["entries"])


# ---------- 展开与过敏原汇总 ----------

def _build_sample(client):
    """构造：曲奇 = 面粉 + 2 次黄油霜；黄油霜 = 黄油 + 糖。"""
    flour = make_ingredient(client, "面粉", ["麸质"])
    butter = make_ingredient(client, "黄油", ["乳"])
    sugar = make_ingredient(client, "砂糖")
    cream = make_recipe(client, "黄油霜", [
        {"ref_type": "ingredient", "ref_id": butter["id"], "quantity": 250, "unit": "g"},
        {"ref_type": "ingredient", "ref_id": sugar["id"], "quantity": 100, "unit": "g"},
    ])
    cookie = make_recipe(client, "曲奇", [
        {"ref_type": "ingredient", "ref_id": flour["id"], "quantity": 300, "unit": "g"},
        {"ref_type": "recipe", "ref_id": cream["id"], "quantity": 1, "unit": "份"},
        {"ref_type": "recipe", "ref_id": cream["id"], "quantity": 1, "unit": "份"},
    ])
    return flour, butter, sugar, cream, cookie


def test_expansion_expands_all_paths(client):
    _, butter, _, cream, cookie = _build_sample(client)
    data = client.get(f"/recipes/{cookie['id']}/expansion").json()

    assert data["recipe_id"] == cookie["id"]
    # 面粉 1 条 + 黄油霜×2 各展开 2 条 = 5 条叶子路径
    assert data["leaf_count"] == 5

    butter_paths = [p for p in data["paths"] if p["ingredient_id"] == butter["id"]]
    assert len(butter_paths) == 2  # 同一子配方用了两次，分别保留
    for leaf in butter_paths:
        assert len(leaf["path"]) == 2                       # 曲奇条目 -> 黄油霜条目
        assert leaf["steps"][0]["ref_id"] == cream["id"]    # 第一跳是子配方
        assert leaf["steps"][1]["ref_id"] == butter["id"]   # 第二跳是原料


def test_reused_sub_recipe_keeps_separate_entry_ids(client):
    _, butter, _, _, cookie = _build_sample(client)
    data = client.get(f"/recipes/{cookie['id']}/expansion").json()

    butter_paths = [p["path"] for p in data["paths"] if p["ingredient_id"] == butter["id"]]
    first_hops = {p[0] for p in butter_paths}
    assert len(first_hops) == 2  # 两次使用各有独立条目编号

    # 过敏原汇总中，每条来源路径也分别列出
    summary = {item["allergen"]: item["sources"] for item in data["allergen_summary"]}
    milk_sources = summary["乳"]
    assert len(milk_sources) == 2
    assert {tuple(s["path"]) for s in milk_sources} == {tuple(p) for p in butter_paths}


def test_allergen_summary_groups_by_allergen(client):
    flour, butter, _, _, cookie = _build_sample(client)
    data = client.get(f"/recipes/{cookie['id']}/expansion").json()
    summary = {item["allergen"]: item["sources"] for item in data["allergen_summary"]}

    assert set(summary) == {"乳", "麸质"}
    assert [s["ingredient_id"] for s in summary["麸质"]] == [flour["id"]]
    assert all(s["ingredient_id"] == butter["id"] for s in summary["乳"])


def test_expansion_of_unknown_recipe_404(client):
    resp = client.get("/recipes/REC-9999/expansion")
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "RECIPE_NOT_FOUND"


def test_deep_nesting_expands_full_chain(client):
    nut = make_ingredient(client, "杏仁", ["坚果"])
    inner = make_recipe(client, "内馅", [{"ref_type": "ingredient", "ref_id": nut["id"]}])
    mid = make_recipe(client, "中层", [{"ref_type": "recipe", "ref_id": inner["id"]}])
    outer = make_recipe(client, "成品", [{"ref_type": "recipe", "ref_id": mid["id"]}])

    data = client.get(f"/recipes/{outer['id']}/expansion").json()
    assert data["leaf_count"] == 1
    leaf = data["paths"][0]
    assert len(leaf["path"]) == 3  # 三层条目编号依次记录
    assert leaf["allergens"] == ["坚果"]
    assert data["allergen_summary"][0]["allergen"] == "坚果"
