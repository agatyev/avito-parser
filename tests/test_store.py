from avito.parse import Listing


def items(*ids):
    return [Listing(id=i, url=f"https://www.avito.ru/items/{i}", title=f"Дом {i}") for i in ids]


def test_remember_returns_only_new(store):
    search = store.add("дома", "https://www.avito.ru/all/doma")
    assert [item.id for item in store.remember(search.id, items("1", "2"))] == ["1", "2"]
    assert [item.id for item in store.remember(search.id, items("2", "3"))] == ["3"]
    assert store.known(search.id) == {"1", "2", "3"}


def test_unsent_holds_until_marked(store):
    search = store.add("дома", "https://www.avito.ru/all/doma")
    store.remember(search.id, items("1", "2"))
    assert len(store.unsent(search.id)) == 2
    store.mark_sent(search.id, ["1"])
    assert [item.id for item in store.unsent(search.id)] == ["2"]
    assert store.waiting() == 1


def test_listing_fields_survive_roundtrip(store):
    search = store.add("дома", "https://www.avito.ru/all/doma")
    original = Listing(id="7", url="https://www.avito.ru/items/7", title="Дом",
                       price=100, price_text="100 ₽", address="Москва",
                       date_text="вчера", description="хороший")
    store.remember(search.id, [original])
    assert store.unsent(search.id)[0] == original


def test_searches_are_independent(store):
    first = store.add("один", "https://www.avito.ru/all/a")
    second = store.add("два", "https://www.avito.ru/all/b")
    store.remember(first.id, items("1"))
    assert [item.id for item in store.remember(second.id, items("1"))] == ["1"]
    assert store.known(first.id) == store.known(second.id) == {"1"}


def test_delete_takes_findings_with_it(store):
    search = store.add("дома", "https://www.avito.ru/all/doma")
    store.remember(search.id, items("1", "2"))
    store.delete(search.id)
    assert store.searches() == []
    assert store.waiting() == 0


def test_toggle_filters_active(store):
    search = store.add("дома", "https://www.avito.ru/all/doma")
    store.toggle(search.id, False)
    assert store.active() == []
    store.toggle(search.id, True)
    assert [item.id for item in store.active()] == [search.id]


def test_remember_ignores_unknown_search(store):
    assert store.remember("deadbeef", items("1")) == []
