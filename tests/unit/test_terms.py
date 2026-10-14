from app.shared.terms import join_terms, split_places, split_terms


def test_split_terms_on_commas_semicolons_and_newlines():
    assert split_terms("Backend Engineer, Java Developer;Go\nRust") == ["Backend Engineer", "Java Developer", "Go", "Rust"]


def test_region_codes_and_countries_stay_with_their_city():
    assert split_places("Victoria, BC, Vancouver, BC, Remote Canada, Toronto, ON, Mississauga, ON") == [
        "Victoria, BC", "Vancouver, BC", "Remote Canada", "Toronto, ON", "Mississauga, ON",
    ]
    assert split_places("Toronto, ON, Canada") == ["Toronto, ON, Canada"]
    assert split_places("London, UK, Austin, TX") == ["London, UK", "Austin, TX"]
    assert split_places("Victoria, British Columbia, Toronto, Ontario") == ["Victoria, British Columbia", "Toronto, Ontario"]
    assert split_places("British Columbia, Ontario") == ["British Columbia, Ontario"]  # leading one stands alone


def test_titles_are_never_merged():
    assert split_terms("Java Developer, QA, Backend Engineer, SRE") == ["Java Developer", "QA", "Backend Engineer", "SRE"]


def test_remote_locations_become_a_keyword():
    from app.services.search import AdzunaQuery, adzuna_query

    assert adzuna_query("Java Developer", "Remote Canada") == AdzunaQuery("Java Developer remote", None)
    assert adzuna_query("Java Developer", "Remote") == AdzunaQuery("Java Developer remote", None)
    assert adzuna_query("Java Developer", "Remote - Vancouver, BC") == AdzunaQuery("Java Developer remote", "Vancouver, BC")
    assert adzuna_query(None, "remote") == AdzunaQuery("remote", None)
    assert adzuna_query("Java Developer", "Victoria, BC") == AdzunaQuery("Java Developer", "Victoria, BC")
    assert adzuna_query("Java Developer", None) == AdzunaQuery("Java Developer", None)


def test_known_locations_are_pinned_and_keywords_ride_along():
    from app.services.search import adzuna_query

    areas = {"victoria bc": ["Canada", "British Columbia", "Capital", "Victoria"]}
    query = adzuna_query("Java Developer", "Victoria,  BC", areas=areas, keywords=["Kafka", "Spring Boot", "kafka"])
    assert query.area == ("Canada", "British Columbia", "Capital", "Victoria")
    assert query.where is None
    assert query.what_or == "Kafka Spring Boot kafka"


def test_normalize_term_ignores_accents_case_and_punctuation():
    from app.shared.terms import normalize_term

    assert normalize_term("Québec") == normalize_term("QUEBEC") == "quebec"
    assert normalize_term("Victoria,  BC") == normalize_term("victoria bc")
    assert normalize_term("C++") != normalize_term("C#") != normalize_term("C")


def test_blank_and_duplicate_terms_are_dropped():
    assert split_terms(" , Java,, java, Java ") == ["Java", "java"]
    assert split_terms("") == []
    assert split_terms(None) == []


def test_join_round_trips():
    terms = ["Victoria, BC", "Vancouver, BC", "Remote"]
    assert split_places(join_terms(terms)) == terms


def test_scheduled_scans_rotate_within_the_daily_budget():
    from app.services.search import queries_per_scan, query_pairs, scan_slice

    pairs = query_pairs(["A", "B", "C", "D"], ["X", "Y", "Z", "W", "V"])  # 20 combinations
    per_scan = queries_per_scan(len(pairs), interval_minutes=60, daily_limit=250)
    assert per_scan == 6  # 60% of 250 over 24 scans/day

    covered = set()
    for hour in range(4):
        covered |= set(scan_slice(pairs, per_scan, 60, now=hour * 3600))
    assert covered == set(pairs)  # every combination within 4 hourly scans
    assert scan_slice(pairs[:3], per_scan, 60) == pairs[:3]  # small searches run whole every scan
