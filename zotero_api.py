from flask import Flask, request, jsonify, send_from_directory
import requests
import fitz  # PyMuPDF
import os
from difflib import get_close_matches
from flask_cors import CORS






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
    Returns a list of collection keys matching the name (including nested ones).
    """
    res = requests.get(f"{ZOTERO_BASE_URL}/users/{user_id}/collections", headers=headers)
    collections = res.json()
    matched = fuzzy_match(collections, name, key="name")

    if not matched:
        return []

    target_keys = {col["data"]["key"] for col in matched}
    parent_to_children = {}
    for col in collections:
        parent = col["data"].get("parentCollection")
        if parent:
            parent_to_children.setdefault(parent, []).append(col["data"]["key"])

    def gather_all_children(keys):
        result = set(keys)
        for k in keys:
            result.update(gather_all_children(parent_to_children.get(k, [])))
        return result

    return list(gather_all_children(target_keys))

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

        # Get user ID
        user_info = requests.get(f"{ZOTERO_BASE_URL}/keys/current", headers=headers).json()
        user_id = user_info["userID"]

        def flatten_collections(collections, parent_map, parent_id=None, prefix=""):
            flat = []
            for col in collections:
                if col.get("parentCollection") == parent_id:
                    full_name = f"{prefix}/{col['data']['name']}".strip("/")
                    col["full_path"] = full_name
                    flat.append(col)
                    flat += flatten_collections(collections, parent_map, col["data"]["key"], full_name)
            return flat

        # Personal collections
        personal_raw = requests.get(
            f"{ZOTERO_BASE_URL}/users/{user_id}/collections", headers=headers
        ).json()
        personal_map = {col["data"]["key"]: col for col in personal_raw}
        personal_flat = flatten_collections(personal_raw, personal_map)
        for col in personal_flat:
            col["library_type"] = "personal"

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
                group_map = {col["data"]["key"]: col for col in group_raw}
                group_flat = flatten_collections(group_raw, group_map)
                for col in group_flat:
                    col["library_type"] = group_name
                group_collections.extend(group_flat)
            except Exception:
                continue  # skip failing group

        return jsonify({
            "personal_collections": personal_flat,
            "group_collections": group_collections
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

        # Resolve collection
        if collection_name:
            if collection_name.startswith("collectionkey:"):
                collection_key = collection_name.split(":", 1)[-1].strip()
                search_params["collection"] = collection_key
            else:
                collection_keys = get_collection_keys_by_name(api_key, user_id, collection_name, headers)
                if collection_keys:
                    search_params["collection"] = ",".join(collection_keys)

        # Initial fetch
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

        # Retry without collection
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

            # Final fallback: split query
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
                    collection_keys = get_collection_keys_by_name(api_key, user_id, collection_name, headers)
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
                    collection_keys = get_collection_keys_by_name(api_key, user_id, collection_name, headers)
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












