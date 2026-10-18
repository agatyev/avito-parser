def card(item_id: str, title: str = "Квартира") -> str:
    return (f'<div data-marker="item" data-item-id="{item_id}">'
            f'<a data-marker="item-title" href="/items/{item_id}">{title} {item_id}</a>'
            f'<meta itemprop="price" content="1000">'
            f'<div data-marker="item-price">1 000 ₽</div>'
            f'<div data-marker="item-address">Москва, Тверская 1</div>'
            f'<div data-marker="item-date">5 минут назад</div>'
            f'</div>')


def listing_page(*item_ids: str) -> str:
    return "<html><body>" + "".join(card(item_id) for item_id in item_ids) + "</body></html>"
