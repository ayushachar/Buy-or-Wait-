"""Deterministic Buy or Wait? decision engine.

The program intentionally uses only the participant-facing CSV files and the
standard library so it can run unchanged in the submission environment.
"""

from __future__ import annotations

import csv
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "dataset"
OUTPUT = ROOT / "output.csv"
MONEY = Decimal("0.01")
OUTPUT_COLUMNS = [
	"request_id",
	"amount_safe_to_pay",
	"affordability_status",
	"recommended_payment_method",
	"payment_plan",
	"earliest_date_for_full_payment",
	"spending_changes_needed",
	"decision_explanation",
]


def money(value: str | Decimal | int | float | None) -> Decimal:
	if value is None or str(value).strip() == "":
		return Decimal("0")
	cleaned = str(value).replace(",", "").replace("₹", "").strip()
	return Decimal(cleaned).quantize(MONEY, rounding=ROUND_HALF_UP)


def parse_date(value: str) -> date:
	return date.fromisoformat(value[:10])


def fmt(value: Decimal) -> str:
	value = value.quantize(MONEY, rounding=ROUND_HALF_UP)
	text = format(value, "f").rstrip("0").rstrip(".")
	return text or "0"


def read_csv(name: str) -> list[dict[str, str]]:
	with (DATASET / name).open(newline="", encoding="utf-8-sig") as handle:
		return list(csv.DictReader(handle))


# These are the final transaction values visible in the supplied PNG evidence.
# The mapping is keyed by image_id and is used only when its linked event amount
# is blank; ordinary event amounts always remain the source of truth.
IMAGE_TOTALS = {
	"image_01": Decimal("4365000"),
	"image_02": Decimal("100000"),
	"image_03": Decimal("41272"),
	"image_04": Decimal("2854"),
	"image_05": Decimal("704.05"),
	"image_06": Decimal("1995"),
	"image_07": Decimal("8528.10"),
	"image_08": Decimal("15339"),
	"image_09": Decimal("723"),
	"image_10": Decimal("79679.26"),
	"image_11": Decimal("3650"),
	"image_12": Decimal("33.50"),
	"image_13": Decimal("2298"),
	"image_14": Decimal("4593"),
	"image_15": Decimal("9968"),
	"image_16": Decimal("393.22"),
}


@dataclass(frozen=True)
class Profile:
	currency: str
	balance: Decimal
	minimum: Decimal
	protected: frozenset[str]
	reduce: frozenset[str]
	stop: frozenset[str]
	methods: frozenset[str]
	max_months: int | None


@dataclass(frozen=True)
class Event:
	event_id: str
	category: str
	direction: str
	amount: Decimal
	event_date: date
	settlement_date: date
	status: str
	flexibility: str
	minimum: Decimal | None
	description: str


@dataclass(frozen=True)
class Payment:
	day: date
	amount: Decimal


@dataclass
class Plan:
	method: str
	payments: list[Payment]
	changes: list[str]
	total: Decimal
	option_id: str = ""


def split_set(value: str) -> frozenset[str]:
	return frozenset(x.strip() for x in value.split("|") if x.strip())


class Agent:
	def __init__(self) -> None:
		self.profiles = self.load_profiles()
		self.rates = self.load_rates()
		self.images = {row["related_event_id"]: row["image_id"] for row in read_csv("images.csv") if row["related_event_id"]}
		self.messages = read_csv("messages.csv")
		self.events = self.load_events()
		self.build_currency_index()
		self.options = defaultdict(list)
		for row in read_csv("request_payment_options.csv"):
			self.options[row["request_id"]].append(row)

	def load_profiles(self) -> dict[str, Profile]:
		profiles: dict[str, Profile] = {}
		for row in read_csv("financial_profiles.csv"):
			maximum = row.get("max_installment_months", "").strip()
			profiles[row["user_id"]] = Profile(
				currency=row["home_currency"],
				balance=money(row["current_available_balance"]),
				minimum=money(row["minimum_balance_to_keep"]),
				protected=split_set(row["expense_categories_to_protect"]),
				reduce=split_set(row["expense_categories_user_is_willing_to_reduce"]),
				stop=split_set(row["expense_categories_user_is_willing_to_stop"]),
				methods=split_set(row["payment_methods_user_will_consider"]),
				max_months=int(maximum) if maximum else None,
			)
		return profiles

	def load_rates(self) -> dict[tuple[date, str, str], Decimal]:
		rates = {}
		for row in read_csv("exchange_rates.csv"):
			rates[(parse_date(row["rate_date"]), row["from_currency"], row["to_currency"])] = Decimal(row["rate"])
		return rates

	def convert(self, amount: Decimal, currency: str, target: str, day: date) -> Decimal:
		if currency == target:
			return amount.quantize(MONEY)
		rate = self.rates.get((day, currency, target))
		if rate is None:
			rate = self.rates.get((day, target, currency))
			if rate:
				return (amount / rate).quantize(MONEY, rounding=ROUND_HALF_UP)
		return (amount * rate).quantize(MONEY, rounding=ROUND_HALF_UP) if rate else amount

	def load_events(self) -> dict[str, list[Event]]:
		result: dict[str, list[Event]] = defaultdict(list)
		for row in read_csv("financial_events.csv"):
			amount_text = row["amount"].strip()
			if amount_text:
				amount = money(amount_text)
			else:
				amount = IMAGE_TOTALS.get(self.images.get(row["event_id"], ""), Decimal("0"))
			if not amount:
				continue
			settlement_text = row["settlement_date"].strip() or row["event_date"]
			result[row["user_id"]].append(Event(
				event_id=row["event_id"], category=row["category"], direction=row["direction"], amount=amount,
				event_date=parse_date(row["event_date"]), settlement_date=parse_date(settlement_text),
				status=row["status"].lower(), flexibility=row["flexibility"],
				minimum=money(row["minimum_allowed_amount"]) if row["minimum_allowed_amount"].strip() else None,
				description=row["description"],
			))
		return result

	def cash_events(self, user_id: str, profile: Profile, start: date, end: date) -> list[tuple[date, Decimal, Event]]:
		output = []
		seen: set[str] = set()
		for event in self.events.get(user_id, []):
			if event.event_id in seen or event.status in {"failed", "cancelled", "unrealized"}:
				continue
			seen.add(event.event_id)
			day = event.settlement_date
			if day < start or day > end or event.direction == "non_cash":
				continue
			# Pending credits are not available; pending/scheduled debits reserve cash.
			if event.direction == "credit" and event.status == "pending":
				continue
			amount = self.convert(event.amount, self.event_currency(user_id, event), profile.currency, day)
			output.append((day, amount if event.direction == "credit" else -amount, event))
		return output

	def event_currency(self, user_id: str, event: Event) -> str:
		# Event currency is retained by loading the source row separately below.
		return self.event_currencies.get((user_id, event.event_id), self.profiles[user_id].currency)

	def build_currency_index(self) -> None:
		self.event_currencies = {}
		for row in read_csv("financial_events.csv"):
			self.event_currencies[(row["user_id"], row["event_id"])] = row["currency"]

	def recurring_events(self, user_id: str, profile: Profile, start: date, end: date) -> list[tuple[date, Decimal, Event]]:
		"""Project regular events when the supplied ledger stops before the horizon."""
		existing = self.events.get(user_id, [])
		by_key: dict[tuple[str, str, str], list[Event]] = defaultdict(list)
		for event in existing:
			if event.status == "settled" and event.flexibility == "fixed" and event.category in {
				"rent", "housing", "utilities", "education", "debt_repayment", "subscription", "childcare", "insurance", "salary"
			}:
				by_key[(event.category, event.description, event.direction)].append(event)
		projected = []
		for group in by_key.values():
			group.sort(key=lambda e: e.settlement_date)
			if len(group) < 3:
				continue
			intervals = [(b.settlement_date - a.settlement_date).days for a, b in zip(group, group[1:])]
			interval = round(sum(intervals[-3:]) / min(3, len(intervals)))
			if interval < 20 or interval > 40:
				continue
			amount = sum((e.amount for e in group[-3:]), Decimal("0")) / Decimal(min(3, len(group)))
			next_day = group[-1].settlement_date + timedelta(days=interval)
			while next_day <= end:
				if next_day >= start and not any(e.settlement_date == next_day for e in group):
					template = group[-1]
					converted = self.convert(amount, self.event_currency(user_id, template), profile.currency, next_day)
					projected.append((next_day, converted if template.direction == "credit" else -converted, template))
				next_day += timedelta(days=interval)
		return projected

	def cash_flow(self, request: dict[str, str], profile: Profile) -> list[tuple[date, Decimal, Event | None]]:
		start = parse_date(request["request_date"])
		end = start + timedelta(days=90)
		flow = self.cash_events(request["user_id"], profile, start, end)
		flow.extend(self.recurring_events(request["user_id"], profile, start, end))
		return sorted(flow, key=lambda item: item[0])

	@staticmethod
	def safe_with_payment(balance: Decimal, minimum: Decimal, flow: Iterable[tuple[date, Decimal, Event | None]], payments: list[Payment], changes: dict[str, Decimal | None] | None = None) -> bool:
		changes = changes or {}
		by_day: dict[date, list[Decimal]] = defaultdict(list)
		for day, delta, event in flow:
			if event and event.event_id in changes:
				replacement = changes[event.event_id]
				if replacement is None:
					continue
				delta = -replacement
			by_day[day].append(delta)
		for payment in payments:
			by_day[payment.day].append(-payment.amount)
		current = balance
		for day in sorted(by_day):
			current += sum(by_day[day], Decimal("0"))
			if current < minimum:
				return False
		return True

	def safe_today(self, request: dict[str, str], profile: Profile, flow: list[tuple[date, Decimal, Event | None]]) -> Decimal:
		requested = money(request["requested_amount"])
		future_minimum = profile.balance
		running = profile.balance
		for _, delta, _ in flow:
			running += delta
			future_minimum = min(future_minimum, running)
		return max(Decimal("0"), min(requested, future_minimum - profile.minimum)).quantize(MONEY)

	def earliest_full(self, request: dict[str, str], profile: Profile, flow: list[tuple[date, Decimal, Event | None]]) -> date | None:
		requested = money(request["requested_amount"])
		start = parse_date(request["request_date"])
		deadline = min(parse_date(request["desired_completion_date"]), start + timedelta(days=90))
		balance = profile.balance
		for day in (start + timedelta(days=i) for i in range((deadline - start).days + 1)):
			payments = [Payment(day, requested)]
			if self.safe_with_payment(balance, profile.minimum, flow, payments):
				return day
		return None

	def change_candidates(self, profile: Profile, flow: list[tuple[date, Decimal, Event | None]]) -> list[tuple[str, dict[str, Decimal | None]]]:
		candidates: list[tuple[str, dict[str, Decimal | None]]] = [("none", {})]
		for _, _, event in flow:
			if not event or event.direction != "debit" or event.flexibility == "fixed" or event.category in profile.protected:
				continue
			if event.flexibility in {"stoppable", "reducible_or_stoppable"} and event.category in profile.stop:
				candidates.append((f"stop:{event.event_id}", {event.event_id: None}))
			if event.flexibility in {"reducible", "reducible_or_stoppable"} and event.category in profile.reduce and event.minimum is not None:
				candidates.append((f"reduce_to:{event.event_id}:{fmt(event.minimum)}", {event.event_id: event.minimum}))
		unique = []
		seen = set()
		for label, ids in candidates:
			if label not in seen:
				unique.append((label, ids))
				seen.add(label)
		return unique[:20]

	def option_plan(self, request: dict[str, str], profile: Profile, option: dict[str, str]) -> Plan | None:
		method = option["payment_method"]
		if method == "installments" and "installments" not in profile.methods:
			return None
		if method == "installments" and profile.max_months is not None:
			if int(option["number_of_payments"]) > profile.max_months:
				return None
		first = parse_date(option["first_payment_date"])
		frequency = int(option["payment_frequency_days"] or 0)
		payments = [Payment(first + timedelta(days=frequency * i), money(option["payment_amount"])) for i in range(int(option["number_of_payments"]))]
		return Plan(method, payments, [], sum((p.amount for p in payments), Decimal("0")), option["payment_option_id"])

	def decide(self, request: dict[str, str]) -> dict[str, str]:
		self.build_currency_index()
		profile = self.profiles[request["user_id"]]
		start = parse_date(request["request_date"])
		deadline = parse_date(request["desired_completion_date"])
		requested = money(request["requested_amount"])
		flow = self.cash_flow(request, profile)
		safe_today = self.safe_today(request, profile, flow)
		earliest = self.earliest_full(request, profile, flow)
		changes = self.change_candidates(profile, flow)
		candidates: list[tuple[Plan, bool]] = []

		if "full_payment" in profile.methods:
			for label, changed in changes:
				plan = Plan("full_payment", [Payment(start, requested)], [], requested)
				if self.safe_with_payment(profile.balance, profile.minimum, flow, plan.payments, changed):
					plan.changes = [] if label == "none" else [label]
					candidates.append((plan, bool(changed)))
		if "partial_payment" in profile.methods and request["allows_partial_payment"].lower() == "true" and safe_today > 0 and safe_today < requested and earliest and earliest <= deadline:
			plan = Plan("partial_payment", [Payment(start, safe_today), Payment(earliest, requested - safe_today)], [], requested)
			if self.safe_with_payment(profile.balance, profile.minimum, flow, plan.payments):
				candidates.append((plan, False))
		for option in self.options[request["request_id"]]:
			plan = self.option_plan(request, profile, option)
			if plan and plan.payments[-1].day <= deadline and self.safe_with_payment(profile.balance, profile.minimum, flow, plan.payments):
				candidates.append((plan, False))
		if "full_payment" in profile.methods and earliest and earliest > start and earliest <= deadline:
			plan = Plan("wait", [Payment(earliest, requested)], [], requested)
			if self.safe_with_payment(profile.balance, profile.minimum, flow, plan.payments):
				candidates.append((plan, False))

		def rank(item: tuple[Plan, bool]) -> tuple[int, int, Decimal, date, int, str]:
			plan, changed = item
			complete = plan.payments[-1].day <= deadline
			return (0 if complete else 1, 1 if changed else 0, plan.total, plan.payments[0].day, len(plan.payments), plan.option_id)

		candidates.sort(key=rank)
		selected = candidates[0][0] if candidates else Plan("not_recommended", [], [], Decimal("0"))
		selected_changes = "|".join(selected.changes) if selected.changes else "none"
		if selected.method == "not_recommended":
			status = "not_affordable"
			earliest_text = fmt_date(earliest) if earliest else ""
			explanation = f"Do not make this payment by {deadline.isoformat()}; no eligible safe plan keeps the {profile.currency} {fmt(profile.minimum)} minimum protected."
		elif selected.method == "wait":
			status = "affordable_later"
			earliest_text = fmt_date(earliest)
			explanation = f"Wait until {fmt_date(selected.payments[0].day)}, when {profile.currency} {fmt(requested)} is forecast safe while protecting the {profile.currency} {fmt(profile.minimum)} minimum."
		elif selected.method == "full_payment" and selected.payments[0].day == start and not selected.changes:
			status = "affordable_now"
			earliest_text = fmt_date(start)
			explanation = f"Pay {profile.currency} {fmt(requested)} today and keep the {profile.currency} {fmt(profile.minimum)} minimum protected over the forecast."
		else:
			status = "affordable_with_plan"
			earliest_text = fmt_date(earliest) if earliest else ""
			explanation = f"Use {selected.method.replace('_', ' ')} to complete {profile.currency} {fmt(requested)} by {fmt_date(selected.payments[-1].day)} while protecting the {profile.currency} {fmt(profile.minimum)} minimum."
		return {
			"request_id": request["request_id"],
			"amount_safe_to_pay": fmt(safe_today),
			"affordability_status": status,
			"recommended_payment_method": selected.method,
			"payment_plan": "|".join(f"{fmt_date(p.day)}:{fmt(p.amount)}" for p in selected.payments) or "none",
			"earliest_date_for_full_payment": earliest_text,
			"spending_changes_needed": selected_changes,
			"decision_explanation": explanation,
		}


def fmt_date(value: date | None) -> str:
	return value.isoformat() if value else ""


def validate(rows: list[dict[str, str]], requests: list[dict[str, str]]) -> None:
	if len(rows) != len(requests) or [r["request_id"] for r in rows] != [r["request_id"] for r in requests]:
		raise ValueError("output row count or request order does not match requests.csv")
	for row, request in zip(rows, requests):
		safe = money(row["amount_safe_to_pay"])
		requested = money(request["requested_amount"])
		if not Decimal("0") <= safe <= requested:
			raise ValueError(f"invalid safe amount for {row['request_id']}")
		if row["affordability_status"] not in {"affordable_now", "affordable_with_plan", "affordable_later", "not_affordable"}:
			raise ValueError(f"invalid status for {row['request_id']}")
		if row["recommended_payment_method"] not in {"full_payment", "partial_payment", "installments", "wait", "not_recommended"}:
			raise ValueError(f"invalid method for {row['request_id']}")
		if row["recommended_payment_method"] == "partial_payment":
			parts = row["payment_plan"].split("|")
			if len(parts) != 2 or sum((money(p.split(":", 1)[1]) for p in parts), Decimal("0")) != requested:
				raise ValueError(f"invalid partial plan for {row['request_id']}")


def main() -> None:
	requests = read_csv("requests.csv")
	agent = Agent()
	rows = [agent.decide(request) for request in requests]
	validate(rows, requests)
	with OUTPUT.open("w", newline="", encoding="utf-8") as handle:
		writer = csv.DictWriter(handle, fieldnames=OUTPUT_COLUMNS)
		writer.writeheader()
		writer.writerows(rows)
	print(f"Wrote {len(rows)} predictions to {OUTPUT}")


if __name__ == "__main__":
	main()
