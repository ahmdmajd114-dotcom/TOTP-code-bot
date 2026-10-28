"""Fixed-column payment writes and location-aware reads of legacy rows.

Only this adapter exposes a normalized A:H view. It retains the physical row
and column of every legacy record so later edits affect the original cells.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import re
from threading import RLock


_WRITE_LOCK = RLock()
_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")


def text(value: object) -> str:
    return str(value if value is not None else "").strip().translate(_DIGITS)


def integer_text(value: object) -> str:
    raw = text(value).replace(",", "").replace("٬", "")
    if not raw:
        return ""
    try:
        amount = Decimal(raw)
    except InvalidOperation:
        return text(value)
    return str(int(amount)) if amount.is_finite() and amount == amount.to_integral_value() else raw


def payment_datetime(value: object) -> datetime:
    """Sheets dates are UTC, including unformatted serial date values."""
    raw = text(value)
    try:
        serial = Decimal(raw)
        if serial.is_finite() and Decimal(20000) <= serial <= Decimal(80000):
            return datetime(1899, 12, 30, tzinfo=timezone.utc) + timedelta(seconds=round(float(serial) * 86400))
    except InvalidOperation:
        pass
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except ValueError:
        pass
    for fmt in (
        "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y",
        "%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M", "%d-%m-%Y",
    ):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    raise ValueError(f"Invalid payment date: {value!r}")


def normalize_record(values: list) -> list[str]:
    row = [text(value) for value in values[:8]]
    row += [""] * (8 - len(row))
    try:
        row[0] = payment_datetime(values[0]).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, IndexError):
        pass
    row[1] = integer_text(row[1])
    row[6] = integer_text(row[6])
    # H can contain unrelated historical cells. Only our explicit IDs count.
    if not row[7].startswith("PAY-"):
        row[7] = ""
    return row


def locate_payment_row(raw: list) -> tuple[list[str], int]:
    """Identify the date/total/method/product/customer/account/chat-id block."""
    candidates = []
    for offset in range(max(1, len(raw) - 6)):
        candidate = normalize_record(raw[offset:offset + 8])
        chat_id = candidate[6]
        has_chat = bool(re.fullmatch(r"-?\d{3,16}", chat_id))
        has_amount = bool(re.fullmatch(r"\d+", candidate[1]))
        has_payment = has_amount and bool(candidate[2])
        has_account = bool(candidate[5])
        try:
            payment_datetime(candidate[0])
            valid_date = True
        except ValueError:
            valid_date = False
        if not candidate[3]:
            continue
        if offset == 0:
            valid = (has_chat and (valid_date or has_payment or has_account)) or (valid_date and has_payment)
        else:
            valid = has_chat and (has_payment or valid_date)
        if valid:
            candidates.append((candidate, offset))
    if not candidates:
        return normalize_record(raw), 0
    # Same record copied twice in a physical row counts only once, prefer A:H.
    unique = {}
    for candidate, offset in candidates:
        key = tuple(candidate[:7])
        unique.setdefault(key, (candidate, offset))
    if len(unique) > 1:
        raise ValueError("Multiple distinct payment records in one row; manual review required")
    return next(iter(unique.values()))


class PaymentWorksheet:
    """The bot's payment-sheet interface. No inferred Google append tables."""

    def __init__(self, worksheet):
        self.worksheet = worksheet
        self.last_shifted_rows = 0
        self.last_undated_paid_rows = 0

    def __getattr__(self, name):
        return getattr(self.worksheet, name)

    def get_all_values(self) -> list[list[str]]:
        raw_rows = self.worksheet.get_all_values(value_render_option="UNFORMATTED_VALUE")
        result = []
        seen_operations = {}
        shifted = undated = 0
        for number, raw in enumerate(raw_rows, start=1):
            if number == 1:
                result.append([text(value) for value in raw[:8]])
                continue
            try:
                row, offset = locate_payment_row(raw)
            except ValueError as exc:
                raise ValueError(f"Payment row {number}: {exc}") from exc
            shifted += bool(offset)
            operation = row[7]
            if operation and operation in seen_operations:
                if seen_operations[operation] != row[:7]:
                    raise ValueError(f"Conflicting payment operation ID at row {number}")
                result.append([""] * 8)
                continue
            if operation:
                seen_operations[operation] = row[:7]
            if row[1].isdigit() and row[2] and row[2] != "ملغاة" and not row[2].startswith("دين"):
                try:
                    payment_datetime(row[0])
                except ValueError:
                    undated += 1
            result.append(row)
        self.last_shifted_rows = shifted
        self.last_undated_paid_rows = undated
        return result

    def row_values(self, row: int) -> list[str]:
        raw = self.worksheet.row_values(row, value_render_option="UNFORMATTED_VALUE")
        return locate_payment_row(raw)[0] if row > 1 else [text(value) for value in raw[:8]]

    def payment_column_offset(self, row: int) -> int:
        raw = self.worksheet.row_values(row, value_render_option="UNFORMATTED_VALUE")
        return locate_payment_row(raw)[1] if row > 1 else 0

    def update_cell(self, row: int, col: int, value):
        if row > 1:
            raw = self.worksheet.row_values(row, value_render_option="UNFORMATTED_VALUE")
            _, offset = locate_payment_row(raw)
        else:
            offset = 0
        return self.worksheet.update_cell(row, offset + col, value)

    def append_row(self, values, value_input_option="USER_ENTERED"):
        """Append at an explicit A:H address, below all existing physical rows."""
        if len(values) > 8:
            raise ValueError("Payments must use columns A:H only")
        with _WRITE_LOCK:
            raw_rows = self.worksheet.get_all_values(value_render_option="UNFORMATTED_VALUE")
            row_number = max(2, len(raw_rows) + 1)
            operation = text(values[7]) if len(values) > 7 else ""
            if operation.startswith("PAY-"):
                for index, raw in enumerate(raw_rows[1:], start=2):
                    existing, _ = locate_payment_row(raw)
                    if existing[7] == operation:
                        if existing[:7] != normalize_record(list(values))[:7]:
                            raise ValueError("Operation ID already used for a different payment")
                        return {"updates": {"updatedRange": f"'{self.title}'!A{index}:H{index}"}}
            if row_number > self.worksheet.row_count:
                self.worksheet.add_rows(max(100, row_number - self.worksheet.row_count))
            row = list(values) + [""] * (8 - len(values))
            self.worksheet.update(
                range_name=f"A{row_number}:H{row_number}", values=[row],
                value_input_option=value_input_option,
            )
            saved = self.worksheet.row_values(row_number, value_render_option="UNFORMATTED_VALUE")
            if normalize_record(saved) != normalize_record(row):
                raise RuntimeError(f"Payment read-back failed at A{row_number}:H{row_number}")
            return {"updates": {"updatedRange": f"'{self.title}'!A{row_number}:H{row_number}"}}
