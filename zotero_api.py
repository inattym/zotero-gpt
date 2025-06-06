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
    Returns a list of collection keys from both personal and group libraries matching the given name,
    including their nested subcollections.
    """
    matched_keys = set()
    parent_to_children = {}

    # --- Personal Collections ---
    personal = requests.get(f"{ZOTERO_BASE_URL}/users/{user_id}/collections", headers=headers).json()
    for col in fuzzy_match(personal, name, key="name"):
        matched_keys.add(col["data"]["key"])
    for col in personal:
        parent = col["data"].get("parentCollection")
        if parent:
            parent_to_children.setdefault(parent, []).append(col["data"]["key"])

    # --- Group Collections ---
    groups = requests.get(f"{ZOTERO_BASE_URL}/users/{user_id}/groups", headers=headers).json()
    for group in groups:
        group_id = group.get("id")
        try:
            group_colls = requests.get(
                f"{ZOTERO_BASE_URL}/groups/{group_id}/collections", headers=headers
            ).json()
            for col in fuzzy_match(group_colls, name, key="name"):
                matched_keys.add(col["data"]["key"])
            for col in group_colls:
                parent = col["data"].get("parentCollection")
                if parent:
                    parent_to_children.setdefault(parent, []).append(col["data"]["key"])
        except Exception:
            continue  # skip failed groups

    # --- Recursive Gathering of Nested Subcollections ---
    def gather_all_children(keys):
        result = set(keys)
        for k in keys:
            result.update(gather_all_children(parent_to_children.get(k, [])))
        return result

    return list(gather_all_children(matched_keys))







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

                def flatten_group(collections, parent_id=None, prefix=""):
                    flat = []
                    for col in collections:
                        if col.get("parentCollection") == parent_id:
                            full_name = f"{prefix}/{col['data']['name']}".strip("/")
                            flat.append({
                                "name": col["data"]["name"],
                                "key": col["data"]["key"],
                                "full_path": full_name,
                                "library_type": group_name
                            })
                            flat += flatten_group(collections, col["data"]["key"], full_name)
                    return flat

                group_flat = flatten_group(group_raw)
                group_collections.extend(group_flat)

            except Exception:
                continue  # skip groups that fail

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

        # Enhanced: resolve full collection + subcollections
        if collection_name:
            if collection_name.startswith("collectionkey:"):
                collection_key = collection_name.split(":", 1)[-1].strip()
                search_params["collection"] = collection_key
            else:
                collection_keys = get_collection_keys_by_name(api_key, user_id, collection_name, headers)
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

        # Step 1: Resolve top-level matching collection(s)
        root_keys = get_collection_keys_by_name(api_key, user_id, collection_name, headers)
        if not root_keys:
            return jsonify({"error": f"No matching collection found for '{collection_name}'"}), 404

        # Step 2: Recursively gather all subcollection keys
        nested_map = get_all_nested_keys(api_key, user_id, headers)
        all_keys = set(root_keys)
        for k in root_keys:
            all_keys.update(nested_map.get(k, []))

        # Step 3: Fetch all items across root + subcollections
        item_res = requests.get(
            f"{ZOTERO_BASE_URL}/users/{user_id}/items",
            headers=headers,
            params={
                "format": "json",
                "collection": ",".join(all_keys),
                "limit": 150
            }
        )
        items = item_res.json()
        if not items:
            return jsonify({"error": "No items found in collection"}), 404

        pdf_summaries = []
        fallback_titles = []

        for item in items:
            data = item.get("data", {})
            key = item.get("key")
            title = data.get("title", "Untitled")
            item_type = data.get("itemType")
            creators = [c.get("lastName", "") for c in data.get("creators", [])]

            if item_type != "attachment":
                # Fetch child attachments
                child_res = requests.get(
                    f"{ZOTERO_BASE_URL}/users/{user_id}/items/{key}/children",
                    headers=headers
                )
                children = child_res.json()
                for child in children:
                    if child["data"].get("itemType") == "attachment" and \
                       child["data"].get("contentType") == "application/pdf":
                        text = extract_pdf_text(api_key, user_id, child["key"], headers)
                        if text:
                            pdf_summaries.append({"title": title, "creators": creators, "text": text})
                        break
            elif data.get("contentType") == "application/pdf":
                text = extract_pdf_text(api_key, user_id, key, headers)
                if text:
                    pdf_summaries.append({"title": title, "creators": creators, "text": text})
            else:
                fallback_titles.append(title)

        if not pdf_summaries:
            return jsonify({
                "note": "No readable PDFs found. Showing fallback titles only.",
                "titles": fallback_titles
            })

        # Basic theme aggregation
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
    res = requests.get(f"{ZOTERO_BASE_URL}/users/{user_id}/collections", headers=headers)
    all_collections = res.json()

    parent_map = {}
    for c in all_collections:
        parent = c["data"].get("parentCollection")
        if parent:
            parent_map.setdefault(parent, []).append(c["data"]["key"])

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











def extract_pdf_text(api_key, user_id, item_key, headers):
    try:
        res = requests.get(
            f"{ZOTERO_BASE_URL}/users/{user_id}/items/{item_key}/file",
            headers=headers,
            stream=True
        )
        if res.status_code != 200:
            return None
        with open("temp_summary.pdf", "wb") as f:
            for chunk in res.iter_content(chunk_size=8192):
                f.write(chunk)
        doc = fitz.open("temp_summary.pdf")
        text = "\n".join([p.get_text() for p in doc])
        doc.close()
        return text if text.strip() else None
    except:
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












