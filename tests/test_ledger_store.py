"""The ledger store contract, run against the in-memory store and the Cosmos store (with a fake container)."""

import copy

import pytest
from azure.cosmos.exceptions import CosmosBatchOperationError, CosmosResourceNotFoundError

from app.ledger.store import AlreadyExists, Conflict, CosmosStore, MemoryStore, NotFound, create, delete, replace


class FakeCosmosContainer:
    """Enough of the SDK's ContainerProxy for the store: items by (partition, id), etags, atomic batches, and the
    handful of query shapes the store writes (the fake reads the SQL the store generated, so a change to that SQL
    that this fake can't honour fails here rather than against the live service)."""

    def __init__(self):
        self.items: dict[tuple[str, str], dict] = {}
        self.counter = 0
        self.queries: list[tuple[str, dict]] = []
        self.batches: list[list] = []

    def _stamp(self, doc):
        self.counter += 1
        stored = copy.deepcopy(doc)
        stored.update(_etag=f'"etag-{self.counter}"', _rid="r", _self="s", _ts=1)
        return stored

    def read(self):
        return {"id": "ledger"}

    def read_item(self, item, partition_key):
        found = self.items.get((partition_key, item))
        if found is None:
            raise CosmosResourceNotFoundError(status_code=404, message="not found")
        return copy.deepcopy(found)

    def execute_item_batch(self, batch_operations, partition_key):
        self.batches.append(batch_operations)
        staged = dict(self.items)
        for index, operation in enumerate(batch_operations):
            action, args = operation[0], operation[1]
            options = operation[2] if len(operation) > 2 else {}
            if action in ("create", "upsert"):
                doc = args[0]
                assert doc["projectId"] == partition_key and not any(k.startswith("_") for k in doc)
                key = (partition_key, doc["id"])
                if action == "create" and key in staged:
                    self._fail(index, 409)
                staged[key] = self._stamp(doc)
            else:
                key = (partition_key, args[0])
                if key not in staged:
                    self._fail(index, 404)
                if "if_match_etag" in options and options["if_match_etag"] != staged[key]["_etag"]:
                    self._fail(index, 412)
                if action == "delete":
                    del staged[key]
                else:
                    assert not any(k.startswith("_") for k in args[1])
                    staged[key] = self._stamp(args[1])
        self.items = staged

    @staticmethod
    def _fail(index, status):
        raise CosmosBatchOperationError(error_index=index, headers={}, status_code=status, message="failed")

    def query_items(self, query, parameters=None, partition_key=None, enable_cross_partition_query=None):
        params = {p["name"]: p["value"] for p in parameters or []}
        self.queries.append((query, params))
        assert partition_key is not None or enable_cross_partition_query, "a query must name a partition or be cross-partition"
        rows = [copy.deepcopy(d) for (pid, _), d in self.items.items() if partition_key in (None, pid)]
        if "c.projectId = @project" in query:
            rows = [d for d in rows if d["projectId"] == params["@project"]]
        if "c.kind = @kind" in query:
            rows = [d for d in rows if d["kind"] == params["@kind"]]
        if "c.kind = 'project'" in query:
            rows = [d for d in rows if d["kind"] == "project"]
        if "c.rid = @rid" in query:
            rows = [d for d in rows if d.get("rid") == params["@rid"]]
        if "c.owner = @owner" in query:
            rows = [d for d in rows if d["owner"] == params["@owner"] or set(d.get("groups", [])) & set(params["@groups"])]
        if "ORDER BY" in query:
            field = query.split("ORDER BY c.")[1].split()[0]
            rows.sort(key=lambda d: d[field], reverse="DESC" in query.split("ORDER BY")[1])
        if "OFFSET" in query:
            rows = rows[params["@offset"] :][: params["@limit"]]
        if query.startswith("SELECT c.id"):
            rows = [{"id": d["id"]} for d in rows]
        return iter(rows)


@pytest.fixture(params=["memory", "cosmos"])
def store(request):
    return MemoryStore() if request.param == "memory" else CosmosStore(FakeCosmosContainer())


def doc(doc_id, kind="requirement", project="p1", **fields):
    return {"id": doc_id, "kind": kind, "projectId": project, **fields}


class TestStoreContract:
    def test_a_created_document_can_be_read_back_with_an_etag(self, store):
        store.apply("p1", [create(doc("req:R1", rid="R1", text="t"))])
        read = store.get("p1", "req:R1")
        assert read["text"] == "t" and read["_etag"]
        assert store.get("p1", "missing") is None and store.get("other", "req:R1") is None

    def test_creating_twice_is_refused(self, store):
        store.apply("p1", [create(doc("a"))])
        with pytest.raises(AlreadyExists):
            store.apply("p1", [create(doc("a"))])

    def test_a_replace_needs_the_current_etag(self, store):
        store.apply("p1", [create(doc("a", n=1))])
        old = store.get("p1", "a")
        store.apply("p1", [replace({**old, "n": 2})])
        with pytest.raises(Conflict):
            store.apply("p1", [replace({**old, "n": 3})])  # a stale copy
        assert store.get("p1", "a")["n"] == 2

    def test_replacing_or_deleting_something_missing_is_not_found(self, store):
        with pytest.raises(NotFound):
            store.apply("p1", [replace({**doc("ghost"), "_etag": '"x"'})])
        with pytest.raises(NotFound):
            store.apply("p1", [delete({**doc("ghost"), "_etag": '"x"'})])

    def test_a_batch_is_all_or_nothing(self, store):
        store.apply("p1", [create(doc("a", n=1))])
        current = store.get("p1", "a")
        with pytest.raises(AlreadyExists):
            store.apply("p1", [replace({**current, "n": 2}), create(doc("b")), create(doc("a"))])
        assert store.get("p1", "a")["n"] == 1 and store.get("p1", "b") is None

    def test_a_stale_etag_in_a_batch_rolls_everything_back(self, store):
        store.apply("p1", [create(doc("a", n=1))])
        stale = store.get("p1", "a")
        store.apply("p1", [replace({**stale, "n": 2})])
        with pytest.raises(Conflict):
            store.apply("p1", [create(doc("new")), replace({**stale, "n": 9})])
        assert store.get("p1", "new") is None

    def test_delete_removes_a_document(self, store):
        store.apply("p1", [create(doc("a"))])
        store.apply("p1", [delete(store.get("p1", "a"))])
        assert store.get("p1", "a") is None

    def test_documents_of_different_projects_are_separate(self, store):
        store.apply("p1", [create(doc("a", rid="R1"))])
        store.apply("p2", [create(doc("a", rid="R1", project="p2"))])
        assert len(store.query("p1", "requirement")) == 1 and len(store.query("p2", "requirement")) == 1

    def test_query_filters_by_kind_and_requirement_and_orders(self, store):
        store.apply("p1", [create(doc(f"ans:R1:{v}", kind="answer", rid="R1", version=v)) for v in (1, 2, 3)])
        store.apply("p1", [create(doc("ans:R2:1", kind="answer", rid="R2", version=1)), create(doc("req:R1", rid="R1"))])
        versions = store.query("p1", "answer", rid="R1", order_by="version", descending=True)
        assert [d["version"] for d in versions] == [3, 2, 1]
        assert len(store.query("p1", "answer")) == 4 and len(store.query("p1", "requirement")) == 1

    def test_query_pages(self, store):
        store.apply("p1", [create(doc(f"evt:{i}", kind="event", at=f"2026-01-0{i}")) for i in range(1, 6)])
        page = store.query("p1", "event", order_by="at", descending=True, limit=2, offset=1)
        assert [d["at"] for d in page] == ["2026-01-04", "2026-01-03"]
        assert len(store.query("p1", "event", order_by="at", offset=3)) == 2

    def test_only_known_fields_can_be_ordered_by(self, store):
        with pytest.raises(ValueError):
            store.query("p1", "event", order_by="id; DROP")

    def test_projects_are_listed_for_their_owner_groups_or_everyone_for_admins(self, store):
        store.apply("p1", [create(doc("project", kind="project", owner="a", groups=["g1"]))])
        store.apply("p2", [create(doc("project", kind="project", project="p2", owner="b", groups=[]))])
        mine = store.list_projects(owner="a", groups=[], everything=False)
        shared = store.list_projects(owner="x", groups=["g1", "g9"], everything=False)
        everything = store.list_projects(owner="x", groups=[], everything=True)
        assert [p["projectId"] for p in mine] == ["p1"]
        assert [p["projectId"] for p in shared] == ["p1"]
        assert store.list_projects(owner="x", groups=["g9"], everything=False) == []
        assert {p["projectId"] for p in everything} == {"p1", "p2"}

    def test_deleting_a_project_removes_all_of_it_and_nothing_else(self, store):
        store.apply("p1", [create(doc(f"d{i}")) for i in range(5)])
        store.apply("p2", [create(doc("keep", project="p2"))])
        assert store.delete_project("p1") == 5
        assert store.query("p1", "requirement") == [] and store.get("p2", "keep")

    def test_a_large_project_is_deleted_in_several_batches(self, store):
        for start in range(0, 250, 100):
            store.apply("p1", [create(doc(f"d{i}")) for i in range(start, min(start + 100, 250))])
        assert store.delete_project("p1") == 250

    def test_a_batch_has_a_size_limit(self, store):
        with pytest.raises(ValueError):
            store.apply("p1", [create(doc(f"d{i}")) for i in range(101)])

    def test_ping_works(self, store):
        store.ping()


class TestCosmosSpecifics:
    def test_a_delete_that_fails_part_way_leaves_the_project_document_so_it_can_be_retried(self):
        container = FakeCosmosContainer()
        store = CosmosStore(container)
        store.apply("p1", [create(doc("project", kind="project", owner="a", groups=[]))])
        for start in range(0, 250, 100):
            store.apply("p1", [create(doc(f"req:{i}")) for i in range(start, min(start + 100, 250))])
        real = container.execute_item_batch
        calls = []

        def flaky(batch_operations, partition_key):
            calls.append(len(batch_operations))
            if len(calls) == 2:
                raise RuntimeError("throttled")
            return real(batch_operations, partition_key)

        container.execute_item_batch = flaky
        with pytest.raises(RuntimeError):
            store.delete_project("p1")

        assert store.get("p1", "project") is not None  # still reachable: the owner can try again
        container.execute_item_batch = real
        assert store.delete_project("p1") == 151 and store.get("p1", "project") is None
    def test_only_the_etag_survives_from_the_services_bookkeeping_fields(self):
        container = FakeCosmosContainer()
        store = CosmosStore(container)
        store.apply("p1", [create(doc("a"))])
        read = store.get("p1", "a")
        assert "_etag" in read and not {"_rid", "_self", "_ts"} & set(read)

    def test_the_etag_is_sent_as_a_condition_not_as_data(self):
        container = FakeCosmosContainer()
        store = CosmosStore(container)
        store.apply("p1", [create(doc("a"))])
        store.apply("p1", [replace(store.get("p1", "a"))])
        action, args, options = container.batches[-1][0]
        assert action == "replace" and "if_match_etag" in options and "_etag" not in args[1]

    def test_the_partition_key_is_added_to_every_document(self):
        container = FakeCosmosContainer()
        CosmosStore(container).apply("p9", [create({"id": "x", "kind": "event"})])
        assert container.items[("p9", "x")]["projectId"] == "p9"

    def test_queries_always_name_their_partition_except_the_project_list(self):
        container = FakeCosmosContainer()
        store = CosmosStore(container)
        store.query("p1", "requirement", order_by="order")
        store.list_projects(owner="a", groups=[], everything=True)
        sql, params = container.queries[0]
        assert "c.projectId = @project" in sql and params["@project"] == "p1"
        assert "ORDER BY c.order ASC" in sql

    def test_user_input_is_always_a_parameter_never_part_of_the_sql(self):
        container = FakeCosmosContainer()
        store = CosmosStore(container)
        store.query("p1' OR 1=1 --", "requirement", rid="R1' OR '1'='1")
        store.list_projects(owner="x' OR 1=1", groups=["g'"], everything=False)
        for sql, _ in container.queries:
            assert "1=1" not in sql and "g'" not in sql and "R1" not in sql
