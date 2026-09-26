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


# ---------- 标签预演（label-preview） ----------

def test_label_preview_reports_added_removed_and_unchanged(client):
    butter = make_ingredient(client, "黄油", ["乳"])
    flour = make_ingredient(client, "面粉", ["麸质"])
    sugar = make_ingredient(client, "砂糖")
    egg = make_ingredient(client, "鸡蛋", ["蛋"])
    recipe = make_recipe(client, "曲奇", [
        {"ref_type": "ingredient", "ref_id": butter["id"]},
        {"ref_type": "ingredient", "ref_id": flour["id"]},
    ])

    # 黄油（乳）移除、鸡蛋（蛋）新增；面粉（麸质）仍在第 2 行，来源不变
    resp = client.post(f"/recipes/{recipe['id']}/label-preview", json={
        "name": "曲奇",
        "entries": [
            {"ref_type": "ingredient", "ref_id": sugar["id"]},
            {"ref_type": "ingredient", "ref_id": flour["id"]},
            {"ref_type": "ingredient", "ref_id": egg["id"]},
        ],
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert data["recipe_id"] == recipe["id"]
    assert data["current"]["recipe_name"] == "曲奇"
    assert data["preview"]["recipe_name"] == "曲奇"

    added = {a["allergen"]: a for a in data["added_allergens"]}
    removed = {a["allergen"]: a for a in data["removed_allergens"]}
    changed = {a["allergen"]: a for a in data["changed_allergens"]}
    assert set(added) == {"蛋"}
    assert set(removed) == {"乳"}
    assert "麸质" not in changed  # 面粉仍在第 2 行，来源原料与位置路径均未变

    # 新增项列出预演侧来源（鸡蛋在第 3 行）
    assert added["蛋"]["after_sources"][0]["ingredient_id"] == egg["id"]
    assert added["蛋"]["after_sources"][0]["path"] == [3]
    # 移除项列出当前侧来源（黄油原在第 1 行）
    assert removed["乳"]["before_sources"][0]["ingredient_id"] == butter["id"]
    assert removed["乳"]["before_sources"][0]["path"] == [1]

    assert data["allergen_list_changed"] is True
    assert data["sources_changed"] is False
    assert data["reprint_recommended"] is True


def test_label_preview_does_not_write(client):
    butter = make_ingredient(client, "黄油", ["乳"])
    flour = make_ingredient(client, "面粉", ["麸质"])
    recipe = make_recipe(client, "曲奇", [
        {"ref_type": "ingredient", "ref_id": butter["id"]},
    ])
    before = client.get(f"/recipes/{recipe['id']}").json()

    resp = client.post(f"/recipes/{recipe['id']}/label-preview", json={
        "name": "新曲奇",
        "entries": [{"ref_type": "ingredient", "ref_id": flour["id"]}],
    })
    assert resp.status_code == 200

    after = client.get(f"/recipes/{recipe['id']}").json()
    assert after == before  # 名称与组成都保持原样
    # 预演侧使用新名称，当前侧仍是旧名称
    body = resp.json()
    assert body["preview"]["recipe_name"] == "新曲奇"
    assert body["current"]["recipe_name"] == "曲奇"
    # 不应发号新条目编号：当前侧保留现有编号，预演侧根配方行尚未落库故为 null
    current_paths = body["current"]["paths"]
    assert current_paths[0]["steps"][0]["entry_id"] == before["entries"][0]["id"]
    assert body["preview"]["paths"][0]["steps"][0]["entry_id"] is None


def test_label_preview_position_paths_keep_duplicate_references(client):
    """同一子配方引用两次：位置路径分别为 [1,...] / [2,...]，重复引用各自保留。"""
    nut = make_ingredient(client, "坚果碎", ["坚果"])
    sub = make_recipe(client, "香酥粒", [
        {"ref_type": "ingredient", "ref_id": nut["id"]},
    ])
    recipe = make_recipe(client, "蛋糕", [
        {"ref_type": "recipe", "ref_id": sub["id"]},
        {"ref_type": "recipe", "ref_id": sub["id"]},
    ])

    resp = client.post(f"/recipes/{recipe['id']}/label-preview", json={
        "name": "蛋糕",
        "entries": [
            {"ref_type": "recipe", "ref_id": sub["id"]},
            {"ref_type": "recipe", "ref_id": sub["id"]},  # 内容相同但条目整体重建
        ],
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()
    # 两侧来源的原料与位置路径一致（数量也未变），过敏原不视为变化
    assert data["added_allergens"] == []
    assert data["removed_allergens"] == []
    assert data["changed_allergens"] == []
    assert data["allergen_list_changed"] is False
    assert data["sources_changed"] is False
    cur_paths = sorted(p["path"] for p in data["current"]["paths"])
    new_paths = sorted(p["path"] for p in data["preview"]["paths"])
    assert cur_paths == new_paths == [[1, 1], [2, 1]]

    # 仅数量 / 单位变化：过敏原、来源原料与路径均未变，无需重印
    resp2 = client.post(f"/recipes/{recipe['id']}/label-preview", json={
        "name": "蛋糕",
        "entries": [
            {"ref_type": "recipe", "ref_id": sub["id"], "quantity": 2, "unit": "份"},
            {"ref_type": "recipe", "ref_id": sub["id"], "quantity": 3, "unit": "份"},
        ],
    })
    data2 = resp2.json()
    assert data2["added_allergens"] == []
    assert data2["removed_allergens"] == []
    assert data2["changed_allergens"] == []
    assert data2["allergen_list_changed"] is False
    assert data2["sources_changed"] is False

    # 开头插入一行无过敏原原料：原第 1 行来源消失、第 3 行为新来源；
    # 第 2 行引用仍在第 2 行，配对为未变化来源。
    filler = make_ingredient(client, "糖粉")  # 无过敏原
    resp3 = client.post(f"/recipes/{recipe['id']}/label-preview", json={
        "name": "蛋糕",
        "entries": [
            {"ref_type": "ingredient", "ref_id": filler["id"]},
            {"ref_type": "recipe", "ref_id": sub["id"]},
            {"ref_type": "recipe", "ref_id": sub["id"]},
        ],
    })
    data3 = resp3.json()
    assert data3["added_allergens"] == []
    assert data3["removed_allergens"] == []
    changed = {a["allergen"]: a for a in data3["changed_allergens"]}
    assert set(changed) == {"坚果"}
    removed_paths = [s["path"] for s in changed["坚果"]["removed_sources"]]
    added_paths = [s["path"] for s in changed["坚果"]["added_sources"]]
    assert removed_paths == [[1, 1]]
    assert added_paths == [[3, 1]]
    assert [s["path"] for s in changed["坚果"]["unchanged_sources"]] == [[2, 1]]
    assert data3["allergen_list_changed"] is False
    assert data3["sources_changed"] is True
    assert data3["reprint_recommended"] is False  # 文字未变，是否重印由工坊判断


def test_label_preview_marks_path_change_via_sub_recipe(client):
    """原料仍存在但经由的配方路径改变：报 changed，并列出变更前后来源。"""
    shared = make_ingredient(client, "调味粉", ["麸质"])
    sub_a = make_recipe(client, "底料A", [{"ref_type": "ingredient", "ref_id": shared["id"]}])
    sub_b = make_recipe(client, "底料B", [{"ref_type": "ingredient", "ref_id": shared["id"]}])
    recipe = make_recipe(client, "成品", [
        {"ref_type": "recipe", "ref_id": sub_a["id"]},
    ])

    resp = client.post(f"/recipes/{recipe['id']}/label-preview", json={
        "name": "成品",
        "entries": [{"ref_type": "recipe", "ref_id": sub_b["id"]}],
    })
    data = resp.json()
    assert data["added_allergens"] == []
    assert data["removed_allergens"] == []
    changed = {a["allergen"]: a for a in data["changed_allergens"]}
    assert set(changed) == {"麸质"}
    item = changed["麸质"]
    assert item["removed_sources"][0]["ingredient_id"] == shared["id"]
    assert item["removed_sources"][0]["path"] == [1, 1]
    assert item["removed_sources"][0]["steps"][0]["ref_id"] == sub_a["id"]
    assert item["added_sources"][0]["ingredient_id"] == shared["id"]
    assert item["added_sources"][0]["path"] == [1, 1]
    assert item["added_sources"][0]["steps"][0]["ref_id"] == sub_b["id"]


def test_label_preview_shared_allergen_partial_change(client):
    """同一过敏原来自两种原料，移除其中一种：仍存在过敏原但来源有增删。"""
    wheat = make_ingredient(client, "小麦粉", ["麸质"])
    soy = make_ingredient(client, "酱油", ["麸质"])
    recipe = make_recipe(client, "酱汁", [
        {"ref_type": "ingredient", "ref_id": wheat["id"]},
        {"ref_type": "ingredient", "ref_id": soy["id"]},
    ])

    resp = client.post(f"/recipes/{recipe['id']}/label-preview", json={
        "name": "酱汁",
        "entries": [{"ref_type": "ingredient", "ref_id": wheat["id"]}],
    })
    data = resp.json()
    assert data["added_allergens"] == []
    assert data["removed_allergens"] == []
    item = {a["allergen"]: a for a in data["changed_allergens"]}["麸质"]
    assert [s["ingredient_id"] for s in item["removed_sources"]] == [soy["id"]]
    assert item["removed_sources"][0]["path"] == [2]
    assert [s["ingredient_id"] for s in item["unchanged_sources"]] == [wheat["id"]]
    assert item["unchanged_sources"][0]["path"] == [1]
    assert item["added_sources"] == []
    assert data["allergen_list_changed"] is False
    assert data["sources_changed"] is True


def test_label_preview_unknown_reference_rejected_like_save(client):
    flour = make_ingredient(client, "面粉", ["麸质"])
    recipe = make_recipe(client, "面团", [
        {"ref_type": "ingredient", "ref_id": flour["id"]},
    ])
    resp = client.post(f"/recipes/{recipe['id']}/label-preview", json={
        "name": "面团",
        "entries": [{"ref_type": "ingredient", "ref_id": "ING-9999"}],
    })
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "UNKNOWN_REFERENCE"
    # 校验失败后原配方不变
    after = client.get(f"/recipes/{recipe['id']}").json()
    assert after["entries"][0]["ref_id"] == flour["id"]


def test_label_preview_cycle_rejected_like_save(client):
    milk = make_ingredient(client, "牛奶", ["乳"])
    a = make_recipe(client, "配方A", [{"ref_type": "ingredient", "ref_id": milk["id"]}])
    b = make_recipe(client, "配方B", [{"ref_type": "recipe", "ref_id": a["id"]}])

    resp = client.post(f"/recipes/{a['id']}/label-preview", json={
        "name": "配方A",
        "entries": [{"ref_type": "recipe", "ref_id": b["id"]}],  # A -> B -> A
    })
    assert resp.status_code == 409
    body = resp.json()["detail"]
    assert body["code"] == "CYCLE_DETECTED"
    assert body["cycle"][0] == a["id"] and body["cycle"][-1] == a["id"]
    # 原配方保持不变
    after = client.get(f"/recipes/{a['id']}").json()
    assert after["entries"][0]["ref_id"] == milk["id"]


def test_label_preview_duplicate_name_rejected_like_save(client):
    flour = make_ingredient(client, "面粉")
    make_recipe(client, "一号面团", [{"ref_type": "ingredient", "ref_id": flour["id"]}])
    other = make_recipe(client, "二号面团", [{"ref_type": "ingredient", "ref_id": flour["id"]}])

    resp = client.post(f"/recipes/{other['id']}/label-preview", json={
        "name": "一号面团",
        "entries": [{"ref_type": "ingredient", "ref_id": flour["id"]}],
    })
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "DUPLICATE_NAME"


def test_label_preview_unknown_recipe_404(client):
    flour = make_ingredient(client, "面粉")
    resp = client.post("/recipes/REC-9999/label-preview", json={
        "name": "幽灵",
        "entries": [{"ref_type": "ingredient", "ref_id": flour["id"]}],
    })
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "RECIPE_NOT_FOUND"
