"""Validate the generated submission against the public output contract."""

import csv
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DATASET = ROOT / "dataset"
OUTPUT = ROOT / "output.csv"
EXPECTED_COLUMNS = [
	"request_id", "amount_safe_to_pay", "affordability_status",
	"recommended_payment_method", "payment_plan", "earliest_date_for_full_payment",
	"spending_changes_needed", "decision_explanation",
]


def money_text(value: Decimal) -> str:
	text = format(value.quantize(Decimal("0.01")), "f").rstrip("0").rstrip(".")
	return text or "0"


def rows(path: Path) -> list[dict[str, str]]:
	with path.open(newline="", encoding="utf-8-sig") as handle:
		return list(csv.DictReader(handle))


def main() -> None:
	requests = rows(DATASET / "requests.csv")
	output = rows(OUTPUT)
	assert list(output[0]) == EXPECTED_COLUMNS
	assert [row["request_id"] for row in output] == [row["request_id"] for row in requests]
	events = {row["event_id"]: row for row in rows(DATASET / "financial_events.csv")}
	options: dict[str, set[str]] = {}
	for option in rows(DATASET / "request_payment_options.csv"):
		payments = []
		first = date.fromisoformat(option["first_payment_date"])
		frequency = int(option["payment_frequency_days"] or 0)
		for index in range(int(option["number_of_payments"])):
	            payments.append(f"{(first + timedelta(days=index * frequency)).isoformat()}:{money_text(Decimal(option['payment_amount']))}")
		options.setdefault(option["request_id"], set()).add("|".join(payments))
	for request, result in zip(requests, output):
		requested = Decimal(request["requested_amount"])
		safe = Decimal(result["amount_safe_to_pay"])
		assert Decimal("0") <= safe <= requested
		assert result["affordability_status"] in {"affordable_now", "affordable_with_plan", "affordable_later", "not_affordable"}
		assert result["recommended_payment_method"] in {"full_payment", "partial_payment", "installments", "wait", "not_recommended"}
		if result["recommended_payment_method"] == "installments":
			assert result["payment_plan"] in options[request["request_id"]]
		if result["recommended_payment_method"] == "partial_payment":
			plan = result["payment_plan"].split("|")
			assert len(plan) == 2
			assert sum((Decimal(item.split(":", 1)[1]) for item in plan), Decimal("0")) == requested
		if result["spending_changes_needed"] != "none":
			for change in result["spending_changes_needed"].split("|"):
				event_id = change.split(":")[1]
				assert event_id in events
				assert events[event_id]["flexibility"] != "fixed"
	print(f"Validated {len(output)} output rows")


if __name__ == "__main__":
	main()
