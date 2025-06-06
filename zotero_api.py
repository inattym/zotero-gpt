from flask import Flask, request, jsonify, send_from_directory
import requests
import fitz  # PyMuPDF
import os
from difflib import get_close_matches
from flask_cors import CORS
import tempfile

import logging
logging.basicConfig(level=logging.DEBUG)










app = Flask(__name__)
CORS(app)
ZOTERO_BASE_URL = "https://api.zotero.org"






# Helper to build auth headers
def get_headers(api_key):
    return {"Zotero-API-Key": api_key}

# Get user ID
def get_user_id(api_key):
    headers = get_headers(api_key)
    res = requests.get(f"{ZOTERO_BASE_URL}/keys/current", headers=headers)
    if res.status_code != 200:
        raise Exception("Invalid API key or Zotero request failed")
    return res.json()["userID"]

def suggest_alternatives(items, q, field="title", n=3):
    """
    Return up to n closest fuzzy matches as suggestions.
    """
    titles = [item.get("data", {}).get(field, "") for item in items]
    matches = get_close_matches(q, titles, n=n, cutoff=0.4)
    return matches

def fuzzy_match(items, query, key="title"):
    """
    Return a list of items whose 'key' field is a close match to the query.
    Preserves ordering by closeness.
    """
    title_map = {item.get("data", {}).get(key, ""): item for item in items}
    titles = list(title_map.keys())
    matches = get_close_matches(query, titles, n=3, cutoff=0.4)
    return [title_map[m] for m in matches]

def fuzzy_match_multi_field(items, query, keys=["title", "abstractNote", "creators"]):
    """
    Fuzzy match items by comparing the query against multiple fields.
    """
    matches = []
    lowered_query = query.lower()

    for item in items:
        data = item.get("data", {})
        combined = ""

        for key in keys:
            if key == "creators":
                names = [creator.get("lastName", "") for creator in data.get("creators", [])]
                combined += " ".join(names) + " "
            else:
                combined += str(data.get(key, "")) + " "

        if lowered_query in combined.lower():
            matches.append(item)

    return matches






def get_collection_keys_by_name(api_key, user_id, name, headers):
    """
    Return all matching collection keys (including nested ones) by fuzzy matching on full_path.
    """
    all_collections = []

    # Personal
    personal = requests.get(f"{ZOTERO_BASE_URL}/users/{user_id}/collections", headers=headers).json()
    for col in personal:
        col["library_type"] = "personal"
    all_collections.extend(personal)

    # Groups
    groups = requests.get(f"{ZOTERO_BASE_URL}/users/{user_id}/groups", headers=headers).json()
    for group in groups:
        gid = group.get("id")
        try:
            group_colls = requests.get(
                f"{ZOTERO_BASE_URL}/groups/{gid}/collections", headers=headers
            ).json()
            for col in group_colls:
                col["library_type"] = f"group_{gid}"
            all_collections.extend(group_colls)
        except Exception:
            continue

    # Build full paths
    id_to_collection = {c["data"]["key"]: c for c in all_collections}
    def build_full_path(col):
        parts = [col["data"]["name"]]
        parent = col["data"].get("parentCollection")
        while parent and parent in id_to_collection:
            parent_col = id_to_collection[parent]
            parts.insert(0, parent_col["data"]["name"])
            parent = parent_col["data"].get("parentCollection")
        return "/".join(parts)

    full_paths = {build_full_path(c).lower(): c for c in all_collections}
    matches = get_close_matches(name.lower(), full_paths.keys(), n=3, cutoff=0.4)
    if not matches:
        return []

    matched_keys = {full_paths[m]["data"]["key"] for m in matches}

    # Build parent-child map
    parent_to_children = {}
    for c in all_collections:
        parent = c["data"].get("parentCollection")
        if parent:
            parent_to_children.setdefault(parent, []).append(c["data"]["key"])

    def gather_all_children(keys):
        result = set(keys)
        for k in keys:
            result.update(gather_all_children(parent_to_children.get(k, [])))
        return result

    result = []
    for key in gather_all_children(matched_keys):
        col = id_to_collection.get(key)
        if col:
            lib_type = col["library_type"]
            if lib_type in ["personal", "user"]:
                result.append({"key": key, "library_type": "user", "library_id": user_id})
            elif lib_type.startswith("group_"):
                gid = int(lib_type.split("_")[1])
                result.append({"key": key, "library_type": "group", "library_id": gid})
    return result











@app.route("/ping", methods=["GET"])
def ping():
    api_key = request.args.get("api_key")
    if not api_key:
        return jsonify({"error": "Missing Zotero API key"}), 400
    try:
        user_id = get_user_id(api_key)
        return jsonify({"status": "ok", "user_id": user_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500






@app.route("/all_collections", methods=["GET"])
def get_all_collections():
    api_key = request.args.get("api_key")
    if not api_key:
        return jsonify({"error": "Missing Zotero API key"}), 400

    try:
        headers = get_headers(api_key)
        user_info = requests.get(f"{ZOTERO_BASE_URL}/keys/current", headers=headers).json()
        user_id = user_info["userID"]

        def flatten_collections(collections, parent_id=None, prefix=""):
            flat = []
            for col in collections:
                if col.get("parentCollection") == parent_id:
                    full_name = f"{prefix}/{col['data']['name']}".strip("/")
                    flat.append({
                        "name": col["data"]["name"],
                        "key": col["data"]["key"],
                        "full_path": full_name,
                        "parent_key": col["data"].get("parentCollection"),
                        "library_type": "personal"
                    })
                    flat += flatten_collections(collections, col["data"]["key"], full_name)
            return flat

        # Personal collections
        personal_raw = requests.get(
            f"{ZOTERO_BASE_URL}/users/{user_id}/collections", headers=headers
        ).json()
        personal_flat = flatten_collections(personal_raw)

        # Group collections
        group_collections = []
        groups = requests.get(
            f"{ZOTERO_BASE_URL}/users/{user_id}/groups", headers=headers
        ).json()

        for group in groups:
            group_id = group.get("id")
            group_name = group.get("name", f"group_{group_id}")
            try:
                group_raw = requests.get(
                    f"{ZOTERO_BASE_URL}/groups/{group_id}/collections", headers=headers
                ).json()

                def flatten_group(collections, parent_id=None, prefix="", group_name="unknown"):
                    flat = []
                    for col in collections:
                        if col.get("parentCollection") == parent_id:
                            full_name = f"{prefix}/{col['data']['name']}".strip("/")
                            flat.append({
                                "name": col["data"]["name"],
                                "key": col["data"]["key"],
                                "full_path": full_name,
                                "parent_key": col["data"].get("parentCollection"),
                                "library_type": group_name
                            })
                            flat += flatten_group(collections, col["data"]["key"], full_name, group_name)
                    return flat

                group_flat = flatten_group(group_raw, parent_id=None, prefix="", group_name=group_name)
                group_collections.extend(group_flat)

            except Exception:
                continue  # skip groups that fail

        return jsonify({
            "personal_collections": personal_flat,
            "group_collections": group_collections
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500







def render_collection_tree(collections):
    from collections import defaultdict

    # Organize collections by parent
    tree = defaultdict(list)
    for col in collections:
        parent = col.get("parent_key") or None
        tree[parent].append(col)

    # Recursive builder
    def walk(parent=None, indent=0):
        lines = []
        for col in sorted(tree.get(parent, []), key=lambda x: x["name"].lower()):
            lines.append("  " * indent + f"- {col['name']}")
            lines.extend(walk(col["key"], indent + 1))
        return lines

    return "\n".join(walk())










@app.route("/collection_tree_preview", methods=["GET"])
def collection_tree_preview():
    api_key = request.args.get("api_key")
    if not api_key:
        return jsonify({"error": "Missing Zotero API key"}), 400

    try:
        headers = get_headers(api_key)
        user_info = requests.get(f"{ZOTERO_BASE_URL}/keys/current", headers=headers).json()
        user_id = user_info["userID"]

        # Helper to build tree from flat collection list
        def build_tree(collections):
            tree = {}
            by_key = {c["data"]["key"]: c["data"] for c in collections}
            children_map = {}
            for c in collections:
                parent = c["data"].get("parentCollection")
                children_map.setdefault(parent, []).append(c["data"]["key"])

            def walk(key, level=0):
                name = by_key[key]["name"]
                indent = "  " * level
                line = f"{indent}- {name}"
                lines = [line]
                for child_key in sorted(children_map.get(key, []), key=lambda k: by_key[k]["name"]):
                    lines.extend(walk(child_key, level + 1))
                return lines

            # Get top-level keys (no parent)
            top_keys = [k for k, v in by_key.items() if not v.get("parentCollection")]
            output = []
            for key in sorted(top_keys, key=lambda k: by_key[k]["name"]):
                output.extend(walk(key))
            return "\n".join(output)

        # Personal collections
        personal_raw = requests.get(f"{ZOTERO_BASE_URL}/users/{user_id}/collections", headers=headers).json()
        personal_tree = build_tree(personal_raw)

        # Group collections
        group_trees = {}
        groups = requests.get(f"{ZOTERO_BASE_URL}/users/{user_id}/groups", headers=headers).json()
        for group in groups:
            gid = group.get("id")
            group_name = group.get("name", f"group_{gid}")
            try:
                group_raw = requests.get(f"{ZOTERO_BASE_URL}/groups/{gid}/collections", headers=headers).json()
                group_trees[group_name] = build_tree(group_raw)
            except Exception:
                continue

        return jsonify({
            "personal_collections_tree": personal_tree,
            "group_collections_tree": group_trees
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500










@app.route("/items", methods=["GET"])
def search_items():
    api_key = request.args.get("api_key")
    q = request.args.get("q", "").strip()
    collection_name = request.args.get("collection", "").strip().lower()

    if not api_key:
        return jsonify({"error": "Missing Zotero API key"}), 400

    try:
        user_id = get_user_id(api_key)
        headers = get_headers(api_key)

        search_params = {
            "format": "json",
            "qmode": "titleCreatorYear",
            "limit": 100
        }

        if q:
            search_params["q"] = q

        # Enhanced: resolve full collection + subcollections
        if collection_name:
            if collection_name.startswith("collectionkey:"):
                collection_key = collection_name.split(":", 1)[-1].strip()
                search_params["collection"] = collection_key
            else:
                collection_refs = get_collection_keys_by_name(api_key, user_id, collection_name, headers)
                collection_keys = [ref["key"] for ref in collection_refs]
                if collection_keys:
                    search_params["collection"] = ",".join(collection_keys)

        # Main search
        item_res = requests.get(
            f"{ZOTERO_BASE_URL}/users/{user_id}/items",
            headers=headers,
            params=search_params
        )
        items = item_res.json()

        if items:
            return jsonify([
                {
                    "title": i["data"].get("title", "Untitled"),
                    "key": i.get("key"),
                    "type": i["data"].get("itemType"),
                    "creators": [c.get("lastName", "") for c in i["data"].get("creators", [])],
                    "abstract": i["data"].get("abstractNote", "")
                }
                for i in items
            ])

        # Retry: broader search ignoring collection
        if q:
            broader_res = requests.get(
                f"{ZOTERO_BASE_URL}/users/{user_id}/items",
                headers=headers,
                params={"format": "json", "limit": 100, "q": q, "qmode": "titleCreatorYear"}
            )
            broader_items = broader_res.json()

            fuzzy_hits = fuzzy_match_multi_field(broader_items, q)
            if fuzzy_hits:
                return jsonify([
                    {
                        "title": i["data"].get("title", "Untitled"),
                        "key": i.get("key"),
                        "type": i["data"].get("itemType"),
                        "creators": [c.get("lastName", "") for c in i["data"].get("creators", [])],
                        "abstract": i["data"].get("abstractNote", "")
                    }
                    for i in fuzzy_hits
                ])

            # Final fallback: split query into words
            keywords = q.split()
            keyword_hits = []
            for word in keywords:
                keyword_hits.extend(fuzzy_match_multi_field(broader_items, word))

            seen = set()
            deduped = []
            for i in keyword_hits:
                k = i.get("key")
                if k and k not in seen:
                    seen.add(k)
                    deduped.append(i)

            if deduped:
                return jsonify([
                    {
                        "title": i["data"].get("title", "Untitled"),
                        "key": i.get("key"),
                        "type": i["data"].get("itemType"),
                        "creators": [c.get("lastName", "") for c in i["data"].get("creators", [])],
                        "abstract": i["data"].get("abstractNote", "")
                    }
                    for i in deduped
                ])

            return jsonify({
                "error": f"No items found for query '{q}'",
                "suggestions": suggest_alternatives(broader_items, q)
            }), 404

        return jsonify({"error": "No items found"}), 404

    except Exception as e:
        return jsonify({"error": str(e)}), 500









@app.route("/summarize_collection", methods=["GET"])
def summarize_collection():
    api_key = request.args.get("api_key")
    collection_name = request.args.get("collection", "").strip().lower()

    if not api_key:
        return jsonify({"error": "Missing Zotero API key"}), 400
    if not collection_name:
        return jsonify({"error": "Missing collection name"}), 400

    try:
        user_id = get_user_id(api_key)
        headers = get_headers(api_key)
        app.logger.debug(f"[summarize_collection] User ID: {user_id}")
        app.logger.debug(f"[summarize_collection] Collection requested: '{collection_name}'")

        # Step 1: Get all matching collection references with library metadata
        collection_refs = get_collection_keys_by_name(api_key, user_id, collection_name, headers)
        app.logger.debug(f"[summarize_collection] Matched collections: {collection_refs}")
        if not collection_refs:
            return jsonify({"error": f"No matching collection found for '{collection_name}'"}), 404

        # Step 2: Load nested collections once
        nested_map = get_all_nested_keys(api_key, user_id, headers)

        # Step 3: Group collections by (library_type, library_id)
        from collections import defaultdict
        grouped_keys = defaultdict(set)
        for ref in collection_refs:
            key = ref["key"]
            lib_type = ref["library_type"]
            lib_id = ref["library_id"]
            grouped_keys[(lib_type, lib_id)].add(str(key))
            grouped_keys[(lib_type, lib_id)].update(map(str, nested_map.get(key, [])))

        pdf_summaries = []
        fallback_titles = []

        # Step 4: Loop through each library
        for (lib_type, lib_id), keys in grouped_keys.items():
            lib_path = f"{lib_type}s/{lib_id}"
            try:
                item_res = requests.get(
                    f"{ZOTERO_BASE_URL}/{lib_path}/items",
                    headers=headers,
                    params={
                        "format": "json",
                        "collection": ",".join(keys),
                        "limit": 200
                    }
                )
                app.logger.debug(f"[summarize_collection] Request URL: {item_res.url}")
                app.logger.debug(f"[summarize_collection] Status Code: {item_res.status_code}")
                items = item_res.json()
            except Exception as e:
                app.logger.error(f"[summarize_collection] Error fetching items: {e}")
                return jsonify({"error": "Failed to fetch items from Zotero"}), 500





            for item in items:
                app.logger.debug(f"[summarize_collection] Processing item: {item.get('data', {}).get('title', 'Untitled')}")
                data = item.get("data", {})
                key = item.get("key")
                title = data.get("title", "Untitled")
                item_type = data.get("itemType")
                creators = [c.get("lastName", "") for c in data.get("creators", [])]

                if item_type != "attachment":
                    fallback_titles.append(title)

                    # Check for child PDFs
                    child_res = requests.get(
                        f"{ZOTERO_BASE_URL}/{lib_path}/items/{key}/children",
                        headers=headers
                    )
                    children = child_res.json()
                    for child in children:
                        cdata = child.get("data", {})
                        if cdata.get("itemType") == "attachment" and cdata.get("contentType") == "application/pdf":
                            text = extract_pdf_text(api_key, user_id, child["key"], headers, lib_type, lib_id)
                            if text:
                                pdf_summaries.append({
                                    "title": title,
                                    "creators": creators,
                                    "text": text
                                })
                elif data.get("contentType") == "application/pdf":
                    text = extract_pdf_text(api_key, user_id, key, headers, lib_type, lib_id)
                    if text:
                        pdf_summaries.append({
                            "title": title,
                            "creators": creators,
                            "text": text
                        })
                else:
                    fallback_titles.append(title)

        if not pdf_summaries:
            return jsonify({
                "note": "No readable PDFs found. Showing fallback titles only.",
                "titles": fallback_titles
            })

        themes = extract_themes([s["text"] for s in pdf_summaries])
        divergence = detect_divergence([s["text"] for s in pdf_summaries])

        return jsonify({
            "collection": collection_name,
            "pdfs_read": len(pdf_summaries),
            "themes": themes,
            "divergent": divergence,
            "docs": [{"title": s["title"], "creators": s["creators"]} for s in pdf_summaries]
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500









def get_all_nested_keys(api_key, user_id, headers):
    """
    Return a mapping of collection keys to their nested subcollection keys,
    across both personal and group libraries.
    """
    all_collections = []

    # Fetch personal collections
    personal = requests.get(
        f"{ZOTERO_BASE_URL}/users/{user_id}/collections", headers=headers
    ).json()
    for col in personal:
        col["library_type"] = "user"
        col["library_id"] = user_id
    all_collections.extend(personal)

    # Fetch group collections
    groups = requests.get(
        f"{ZOTERO_BASE_URL}/users/{user_id}/groups", headers=headers
    ).json()

    for group in groups:
        gid = group.get("id")
        try:
            group_colls = requests.get(
                f"{ZOTERO_BASE_URL}/groups/{gid}/collections", headers=headers
            ).json()
            for col in group_colls:
                col["library_type"] = "group"
                col["library_id"] = gid
            all_collections.extend(group_colls)
        except Exception:
            continue

    # Build parent-child map
    parent_map = {}
    for c in all_collections:
        parent = c["data"].get("parentCollection")
        if parent:
            parent_map.setdefault(parent, []).append(c["data"]["key"])

    # Recursive gatherer
    def gather(k):
        out = set()
        children = parent_map.get(k, [])
        out.update(children)
        for c in children:
            out.update(gather(c))
        return out

    nested = {}
    for c in all_collections:
        k = c["data"]["key"]
        nested[k] = gather(k)

    return nested











def extract_pdf_text(api_key, user_id, item_key, headers, lib_type="user", lib_id=None):
    """
    Download and extract text from a Zotero PDF attachment.
    Supports both user and group libraries. Uses fitz for PDF parsing.
    """
    try:
        if lib_type == "user":
            lib_id = user_id
        lib_path = f"{lib_type}s/{lib_id}"

        file_url = f"{ZOTERO_BASE_URL}/{lib_path}/items/{item_key}/file"
        res = requests.get(file_url, headers=headers, stream=True)

        if res.status_code != 200:
            return None

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            for chunk in res.iter_content(chunk_size=8192):
                if chunk:
                    tmp.write(chunk)
            tmp_path = tmp.name

        doc = fitz.open(tmp_path)
        text = "\n".join([p.get_text() for p in doc])
        doc.close()

        return text.strip() if text.strip() else None

    except Exception as e:
        print(f"[extract_pdf_text ERROR] {e}")
        return None







def extract_themes(texts):
    keywords = ["equity", "lab", "sensemaking", "argument", "TA", "instruction", "framework"]
    found = set()
    for t in texts:
        for k in keywords:
            if k in t.lower():
                found.add(k)
    return list(found)


def detect_divergence(texts):
    # Dummy check: find outliers by word count
    if len(texts) < 2:
        return []
    lengths = [len(t.split()) for t in texts]
    avg = sum(lengths) / len(lengths)
    outliers = []
    for i, l in enumerate(lengths):
        if abs(l - avg) > 0.5 * avg:
            outliers.append(f"Doc {i+1} is unusually {'long' if l > avg else 'short'}")
    return outliers











@app.route("/notes", methods=["GET"])
def get_notes():
    api_key = request.args.get("api_key")
    item_key = request.args.get("itemKey")
    query = request.args.get("q", "").strip()
    collection_name = request.args.get("collection", "").strip().lower()

    if not api_key:
        return jsonify({"error": "Missing Zotero API key"}), 400

    try:
        user_id = get_user_id(api_key)
        headers = get_headers(api_key)

        # Step 1: Resolve itemKey using query if not provided
        if not item_key and query:
            search_params = {
                "format": "json",
                "q": query,
                "qmode": "titleCreatorYear",
                "limit": 50
            }

            # Apply collection filter
            if collection_name:
                if collection_name.startswith("collectionkey:"):
                    search_params["collection"] = collection_name.split(":", 1)[-1].strip()
                else:
                    collection_refs = get_collection_keys_by_name(api_key, user_id, collection_name, headers)
                    collection_keys = [ref["key"] for ref in collection_refs]
                    if collection_keys:
                        search_params["collection"] = ",".join(collection_keys)

            # Initial search
            search_res = requests.get(
                f"{ZOTERO_BASE_URL}/users/{user_id}/items",
                headers=headers,
                params=search_params
            )
            items = search_res.json()

            # Fallback broader search if nothing found
            if not items and query:
                fallback_res = requests.get(
                    f"{ZOTERO_BASE_URL}/users/{user_id}/items",
                    headers=headers,
                    params={"format": "json", "q": query, "limit": 100}
                )
                items = fallback_res.json()

            # Try multi-field fuzzy match
            fuzzy_matches = fuzzy_match_multi_field(items, collection_name or query)
            if fuzzy_matches:
                item_key = fuzzy_matches[0].get("key")

            # Suggest candidates if nothing resolved
            if not item_key and items:
                return jsonify({
                    "message": f"No exact match for '{query}'",
                    "candidates": [
                        {"title": i["data"].get("title", "Untitled"), "key": i["key"]}
                        for i in items[:3]
                    ]
                }), 404

        if not item_key:
            return jsonify({"error": "Missing itemKey or failed to resolve query"}), 404

        # Step 2: Retrieve notes (children) for the itemKey
        notes_res = requests.get(
            f"{ZOTERO_BASE_URL}/users/{user_id}/items/{item_key}/children",
            headers=headers,
            params={"itemType": "note"}
        )
        notes = notes_res.json()

        if not notes:
            return jsonify({"message": "No notes found for this item."}), 204

        return jsonify(notes)

    except Exception as e:
        return jsonify({"error": str(e)}), 500










@app.route("/read_pdf", methods=["GET"])
def read_pdf():
    api_key = request.args.get("api_key")
    item_key = request.args.get("itemKey")
    title = request.args.get("title", "").strip()
    collection_name = request.args.get("collection", "").strip().lower()

    if not api_key:
        return jsonify({"error": "Missing api_key"}), 400

    try:
        headers = get_headers(api_key)
        user_id = get_user_id(api_key)

        # Step 1: Resolve itemKey from title if not provided
        if not item_key and title:
            search_params = {
                "format": "json",
                "q": title,
                "qmode": "title",
                "limit": 5
            }

            # Resolve collection key(s)
            if collection_name:
                if collection_name.startswith("collectionkey:"):
                    search_params["collection"] = collection_name.split(":", 1)[-1].strip()
                else:
                    collection_refs = get_collection_keys_by_name(api_key, user_id, collection_name, headers)
                    collection_keys = [ref["key"] for ref in collection_refs]
                    if collection_keys:
                        search_params["collection"] = ",".join(collection_keys)

            item_res = requests.get(
                f"{ZOTERO_BASE_URL}/users/{user_id}/items",
                headers=headers,
                params=search_params
            )
            items = item_res.json()

            if not items:
                # Try broader match
                fallback_res = requests.get(
                    f"{ZOTERO_BASE_URL}/users/{user_id}/items",
                    headers=headers,
                    params={"format": "json", "q": title, "limit": 25}
                )
                items = fallback_res.json()
                items = fuzzy_match_multi_field(items, title)

            if items:
                item_key = items[0].get("key")
            else:
                return jsonify({
                    "error": f"Could not resolve itemKey for title '{title}'",
                    "candidates": [
                        {"title": i["data"].get("title", "Untitled"), "key": i["key"]}
                        for i in items[:3]
                    ]
                }), 404

        if not item_key:
            return jsonify({"error": "Missing itemKey"}), 400

        # Step 2: Get metadata and determine library scope
        item_res = requests.get(
            f"{ZOTERO_BASE_URL}/users/{user_id}/items/{item_key}",
            headers=headers
        )

        if item_res.status_code != 200:
            return jsonify({"error": "Could not retrieve item metadata"}), item_res.status_code

        item_data = item_res.json()
        item_type = item_data["data"]["itemType"]
        library = item_data["library"]
        library_type = library["type"]
        library_id = library["id"]

        # Step 3: If not an attachment, search children for PDF
        if item_type != "attachment":
            children_res = requests.get(
                f"{ZOTERO_BASE_URL}/{library_type}s/{library_id}/items/{item_key}/children",
                headers=headers
            )
            children = children_res.json()
            pdfs = [
                c for c in children
                if c["data"].get("itemType") == "attachment" and
                   c["data"].get("contentType") == "application/pdf"
            ]
            if not pdfs:
                return jsonify({"error": "No PDF attachment found for this item"}), 404
            item_key = pdfs[0]["key"]

        # Step 4: Download and extract PDF
        file_res = requests.get(
            f"{ZOTERO_BASE_URL}/{library_type}s/{library_id}/items/{item_key}/file",
            headers=headers,
            stream=True
        )
        if file_res.status_code != 200:
            return jsonify({"error": "Could not download PDF file"}), file_res.status_code

        with open("temp.pdf", "wb") as f:
            for chunk in file_res.iter_content(chunk_size=8192):
                f.write(chunk)

        doc = fitz.open("temp.pdf")
        text = "\n".join([page.get_text() for page in doc])
        doc.close()

        if not text.strip():
            return jsonify({"error": "PDF extracted but contains no readable text."}), 204

        return jsonify({
            "title": title or item_key,
            "text": text[:15000]  # Trimmed for safety
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500






# Optional endpoint: extract themes + divergence from raw text (outside Zotero)

"""
@app.route("/extract_themes", methods=["POST"])
def extract_themes_endpoint():
    data = request.json
    texts = data.get("texts", [])

    if not texts or not isinstance(texts, list):
        return jsonify({"error": "Provide a list of 'texts'"}), 400

    themes = extract_themes(texts)
    divergence = detect_divergence(texts)

    return jsonify({
        "themes": themes,
        "divergent": divergence
    })
"""







# Serve static files
@app.route("/openapi.yaml")
def serve_openapi():
    return send_from_directory(os.getcwd(), "openapi.yaml", mimetype="text/yaml")

@app.route("/logo.png")
def serve_logo():
    return send_from_directory(os.getcwd(), "logo.png", mimetype="image/png")

@app.route("/privacy", methods=["GET"])
def serve_privacy():
    return send_from_directory(os.getcwd(), "privacy.html", mimetype="text/html")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)












