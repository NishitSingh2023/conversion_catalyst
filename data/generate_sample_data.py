#!/usr/bin/env python
"""Generate synthetic sample data for the Lead Assignment Engine.

The generator fabricates data with *real, learnable structure* so that the
downstream XGBoost model and the assignment optimizer have something meaningful
to work with in the demo:

  * Each manager covers a subset of languages / geographies / products and has a
    latent "skill" level.
  * A lead's ground-truth conversion probability depends on its intent bucket
    plus how well the handling manager matches on language, geography and
    product, plus the manager's skill. Conversions are then sampled from that
    probability. This is exactly the signal the model is meant to recover.

Two CSVs are written to data/sample/:
  * lead_manager_history.csv - historical (lead, manager, converted?) triples
  * new_leads.csv            - a fresh batch to be assigned

The new-leads batch is deliberately larger than total manager capacity so the
pool-overflow path is exercised in the demo.

Usage:
    python data/generate_sample_data.py --managers 80 --history 24000 --new-leads 5000
"""
from __future__ import annotations

import argparse
import csv
import random
from datetime import date, timedelta
from pathlib import Path

OUT_DIR = Path(__file__).resolve().parent / "sample"

LANGUAGES = ["English", "Hindi", "Tamil", "Telugu", "Kannada", "Marathi", "Bengali", "Gujarati"]
GEOGRAPHIES = [
    "Delhi", "Mumbai", "Bengaluru", "Chennai", "Hyderabad", "Pune",
    "Kolkata", "Ahmedabad", "Jaipur", "Lucknow",
]
PRODUCTS = ["JEE", "NEET", "Foundation-8", "Foundation-9", "CBSE-10", "CBSE-12", "State-Board", "Olympiad"]
GRADES = ["8", "9", "10", "11", "12"]
SOURCES = ["organic", "paid_search", "social", "referral", "website", "app"]
PARENT_STUDENT = ["parent", "student"]
INTENTS = ["H", "M", "L", "EL"]

# Base conversion rate by intent bucket (ground truth used only for generation).
BASE_RATE = {"H": 0.34, "M": 0.17, "L": 0.07, "EL": 0.02}


def make_managers(n: int, rng: random.Random) -> list[dict]:
    managers = []
    for i in range(n):
        managers.append(
            {
                "manager_id": f"MGR{i:04d}",
                "languages": rng.sample(LANGUAGES, rng.randint(1, 3)),
                "geographies": rng.sample(GEOGRAPHIES, rng.randint(1, 4)),
                "products": rng.sample(PRODUCTS, rng.randint(2, 5)),
                "skill": rng.uniform(0.0, 0.15),          # latent ability
                "base_response": rng.uniform(5, 120),     # avg first-response mins
            }
        )
    return managers


def true_conversion_prob(lead: dict, mgr: dict) -> float:
    p = BASE_RATE[lead["intent_bucket"]]
    if lead["language"] in mgr["languages"]:
        p += 0.12
    if lead["geography"] in mgr["geographies"]:
        p += 0.10
    if lead["product_interest"] in mgr["products"]:
        p += 0.08
    p += mgr["skill"]
    return max(0.01, min(0.95, p))


def random_lead(rng: random.Random, lead_id: str, bias_mgr: dict | None = None) -> dict:
    """Draw a lead. If bias_mgr is given, attributes lean toward that manager's
    coverage (simulating that the manager historically handled such leads)."""
    if bias_mgr and rng.random() < 0.55:
        language = rng.choice(bias_mgr["languages"])
        geography = rng.choice(bias_mgr["geographies"])
        product = rng.choice(bias_mgr["products"])
    else:
        language = rng.choice(LANGUAGES)
        geography = rng.choice(GEOGRAPHIES)
        product = rng.choice(PRODUCTS)

    # Intent distribution skewed toward M/L (H and EL rarer), like real funnels.
    intent = rng.choices(INTENTS, weights=[0.15, 0.35, 0.35, 0.15])[0]
    return {
        "lead_id": lead_id,
        "intent_bucket": intent,
        "geography": geography,
        "language": language,
        "product_interest": product,
        "lead_source": rng.choice(SOURCES),
        "grade": rng.choice(GRADES),
        "parent_student": rng.choice(PARENT_STUDENT),
    }


def generate_history(managers: list[dict], rows: int, rng: random.Random) -> list[dict]:
    history = []
    today = date.today()
    per_mgr = max(1, rows // len(managers))
    counter = 0
    for mgr in managers:
        # Vary how recently active each manager is (drives derived_active_flag).
        recency_bias = rng.choice([3, 7, 15, 25, 45, 70])
        for _ in range(per_mgr):
            counter += 1
            lead = random_lead(rng, f"HL{counter:07d}", bias_mgr=mgr)
            p = true_conversion_prob(lead, mgr)
            converted = rng.random() < p
            response = max(1.0, rng.gauss(mgr["base_response"], 20))
            days_ago = rng.randint(0, recency_bias)
            history.append(
                {
                    "lead_id": lead["lead_id"],
                    "manager_id": mgr["manager_id"],
                    "lead_intent_bucket": lead["intent_bucket"],
                    "lead_geography": lead["geography"],
                    "lead_language": lead["language"],
                    "lead_product": lead["product_interest"],
                    "lead_source": lead["lead_source"],
                    "lead_grade": lead["grade"],
                    "contact_attempts": rng.randint(1, 6),
                    "first_response_mins": round(response, 1),
                    "converted": converted,
                    "interaction_date": (today - timedelta(days=days_ago)).isoformat(),
                }
            )
    rng.shuffle(history)
    return history


def generate_new_leads(count: int, rng: random.Random, batch_id: str) -> list[dict]:
    leads = []
    for i in range(count):
        lead = random_lead(rng, f"NL{i:07d}")
        lead["batch_id"] = batch_id
        leads.append(lead)
    return leads


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    print(f"wrote {len(rows):>7,} rows -> {path}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--managers", type=int, default=80)
    parser.add_argument("--history", type=int, default=24000)
    parser.add_argument("--new-leads", type=int, default=5000)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--batch-id", default=date.today().isoformat())
    args = parser.parse_args()

    rng = random.Random(args.seed)
    managers = make_managers(args.managers, rng)
    history = generate_history(managers, args.history, rng)
    new_leads = generate_new_leads(args.new_leads, rng, args.batch_id)

    write_csv(OUT_DIR / "lead_manager_history.csv", history)
    write_csv(OUT_DIR / "new_leads.csv", new_leads)

    conv = sum(1 for h in history if h["converted"])
    print(
        f"\nSummary: {args.managers} managers | {len(history):,} history rows "
        f"({conv:,} converted, {conv / len(history):.1%}) | {len(new_leads):,} new leads "
        f"| batch_id={args.batch_id}"
    )
    print(f"Manager capacity = {args.managers} x 50 = {args.managers * 50:,}; "
          f"new leads = {len(new_leads):,} -> pool overflow expected "
          f"if eligible capacity < {len(new_leads):,}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
