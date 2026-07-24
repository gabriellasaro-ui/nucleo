"""Currency formatting shared by the `money` template filter and the views.

One source of truth so the dashboard KPIs (formatted in Python) and the
templates (formatted with `|money`) always agree.
"""

CURRENCY_SYMBOLS = {"BRL": "R$", "USD": "$", "EUR": "€"}


def format_money(value, code="BRL"):
    """Symbol + locale grouping. BRL/EUR use dot thousands ("R$ 1.234"),
    USD uses comma thousands with no space ("$1,234")."""
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return value
    symbol = CURRENCY_SYMBOLS.get(code, "R$")
    whole = "{:,.0f}".format(abs(number))       # 1,234
    if code in ("BRL", "EUR"):
        whole = whole.replace(",", ".")          # 1.234
    sign = "-" if number < 0 else ""
    if code == "USD":
        return f"{sign}{symbol}{whole}"           # $1,234
    return f"{sign}{symbol} {whole}"              # R$ 1.234 / € 1.234
