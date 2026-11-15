"""Keep authentication state, discarding known VK media/API caches."""
from __future__ import annotations


CACHE_DATABASES = frozenset({
    "LiveIndexedDB_metadata", "api_cache", "data_cache", "emoji-db-v1",
    "reforged-db-v1", "stickers-ugc-v1", "stickers-v1",
})


def compact_session(state: dict) -> dict:
    result = dict(state)
    result["origins"] = []
    for original in state.get("origins", []):
        origin = dict(original)
        if "indexedDB" in original:
            databases = []
            for original_database in original["indexedDB"]:
                if original_database.get("name") in CACHE_DATABASES:
                    continue
                database = dict(original_database)
                stores = []
                for original_store in database.get("stores", []):
                    store = dict(original_store)
                    store["records"] = [record for record in original_store.get("records", [])
                                        if not (isinstance(record.get("key"), str) and
                                                record["key"].endswith("-stickers-keywords"))]
                    stores.append(store)
                database["stores"] = stores
                databases.append(database)
            origin["indexedDB"] = databases
        result["origins"].append(origin)
    return result
