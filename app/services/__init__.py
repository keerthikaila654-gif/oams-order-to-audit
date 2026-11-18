class BusinessError(Exception):
    """A rule of the business was violated; message is safe to show to the user."""


class InsufficientStock(BusinessError):
    def __init__(self, item_name, available_m, required_m, unit=""):
        from ..money import fmt_qty
        self.item_name, self.available_m, self.required_m = item_name, available_m, required_m
        self.shortage_m = required_m - available_m
        super().__init__(
            f"Insufficient stock for {item_name}: required {fmt_qty(required_m)} {unit}, "
            f"available {fmt_qty(available_m)} {unit}, shortage {fmt_qty(self.shortage_m)} {unit}")
