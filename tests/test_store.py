import pytest

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


def test_new_recipient_gets_every_search_and_every_search_gets_everyone(store):
    first = store.add("мини пк", "https://www.avito.ru/all/a")
    store.add_recipient("me@example.com")
    second = store.add("дачи", "https://www.avito.ru/all/b")
    assert sorted(store.recipients()[0].searches) == sorted([first.id, second.id])
    assert store.subscribers(second.id) == ["me@example.com"]


def test_subscriptions_are_per_address(store):
    pc = store.add("мини пк", "https://www.avito.ru/all/a")
    dacha = store.add("дачи", "https://www.avito.ru/all/b")
    store.add_recipient("me@example.com")
    store.add_recipient("work@example.com")
    store.subscribe("work@example.com", dacha.id, False)
    assert store.subscribers(pc.id) == ["me@example.com", "work@example.com"]
    assert store.subscribers(dacha.id) == ["me@example.com"]
    store.subscribe("work@example.com", dacha.id, True)
    assert store.subscribers(dacha.id) == ["me@example.com", "work@example.com"]


def test_removed_address_and_deleted_search_take_subscriptions_with_them(store):
    search = store.add("дачи", "https://www.avito.ru/all/b")
    store.add_recipient("me@example.com")
    store.remove_recipient("me@example.com")
    assert store.recipients() == []
    assert store.subscribers(search.id) == []
    store.add_recipient("me@example.com")
    store.delete(search.id)
    assert store.recipients()[0].searches == []


def test_unknown_address_or_search_cannot_be_subscribed(store):
    search = store.add("дачи", "https://www.avito.ru/all/b")
    with pytest.raises(ValueError):
        store.subscribe("nobody@example.com", search.id, True)
    store.add_recipient("me@example.com")
    with pytest.raises(ValueError):
        store.subscribe("me@example.com", "deadbeef", True)


def test_same_address_is_added_once(store):
    store.add_recipient("me@example.com")
    with pytest.raises(ValueError):
        store.add_recipient("me@example.com")


def test_seed_only_fills_an_empty_mailing(store):
    search = store.add("дачи", "https://www.avito.ru/all/b")
    store.seed_recipients(["first@example.com", "second@example.com"])
    store.seed_recipients(["third@example.com"])
    assert [person.email for person in store.recipients()] == \
        ["first@example.com", "second@example.com"]
    assert store.subscribers(search.id) == ["first@example.com", "second@example.com"]
