"""Employers with open jobs in Canada outside tech -- construction, health and
care, logistics, trades, food, retail, housing -- offered on Platform -> Job
sources so the pool isn't only tech companies (the job board's search found
admin jobs, but the pool's companies had almost none).

Each was checked on 2026-10-21 by reading its public feed: (provider, board,
name, Canadian jobs then). Feeds change: one that has closed simply shows an
error on the Job sources page and can be removed there.
"""
from __future__ import annotations

SUGGESTED: tuple[tuple[str, str, str, int], ...] = (
    ("workable", "community-sports-partners", "Community Sports Partners", 70),
    ("workable", "voyago", "Voyago", 60),
    ("workable", "the-care-company-inc", "The Care Company", 59),
    ("workable", "rentokil-initial", "Rentokil Initial", 51),
    ("workable", "durham-community-health-centre", "Durham Community Health Centre", 50),
    ("workable", "the-pod-group", "The Pod Group", 46),
    ("workable", "cecs", "CECS", 43),
    ("workable", "medallion-energy-services", "Medallion Energy Services", 42),
    ("workable", "algaecal", "AlgaeCal", 42),
    ("workable", "charger-logistics-inc", "Charger Logistics", 39),
    ("workable", "csncollision", "CSN Collision", 37),
    ("workable", "maple-reinders", "Maple Reinders", 36),
    ("workable", "barorh", "Baro RH", 26),
    ("workable", "peterlucas", "Peter Lucas Project Management", 25),
    ("workable", "lamour", "Lamour", 19),
    ("workable", "matrix-camps-and-logistics", "Matrix Camps and Logistics", 18),
    ("workable", "tayco", "Tayco", 15),
    ("workable", "denison-mines", "Denison Mines", 14),
    ("workable", "john-brooks-company", "John Brooks Company", 14),
    ("workable", "pacifica-housing", "Pacifica Housing", 13),
    ("workable", "spirit-omega-inc", "Spirit Omega", 13),
    ("workable", "delmar-international-inc", "Delmar International", 13),
    ("workable", "professionalexcavators", "Professional Excavators & Construction", 13),
    ("workable", "eminent-valet", "Eminent Valet", 12),
    ("workable", "awcsolutions", "AWC Process Solutions", 12),
    ("workable", "auto-electric-service", "Auto Electric Service", 11),
    ("workable", "nfi-group-inc", "NFI Group", 11),
    ("workable", "burnoutbrands", "Burnout Brands", 11),
    ("workable", "victor-energy", "Victor Energy", 9),
    ("workable", "tommy-guns-original-barbershop", "Tommy Gun's Original Barbershop", 89),
    ("workable", "trivium", "Trivium", 8),
    ("workable", "talltreehealth", "Tall Tree Health", 8),
    ("workable", "winnipeg-football-club", "Winnipeg Football Club", 7),
    ("workable", "silver-hills-bakery", "Silver Hills Bakery", 7),
    ("workable", "hungry-marketplace", "HUNGRY Marketplace", 7),
    ("workable", "cts-foods-brokers-inc", "CTS Foods Brokers", 6),
    ("workable", "arctic-arrow-group", "Arctic Arrow Group", 6),
    ("workable", "park-paving-ltd", "Park Paving", 5),
    ("workable", "veritree", "veritree", 5),
    ("smartrecruiters", "ubisoft2", "Ubisoft", 134),
    ("smartrecruiters", "equinox", "Equinox", 19),
    ("smartrecruiters", "boschgroup", "Bosch", 7),
    ("smartrecruiters", "averydennison", "Avery Dennison", 5),
    ("greenhouse", "mejuri", "Mejuri", 34),
    ("lever", "knix", "Knix", 48),
)
