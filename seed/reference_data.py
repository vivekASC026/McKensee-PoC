"""Static reference data for the seed script.

The ZIP / city / state triples exist so the profiler has a real functional
dependency to find. Each ZIP maps to exactly one city and one state.
"""

from __future__ import annotations

# (city, state, base ZIP). Each anchor is expanded into a few neighboring ZIPs
# that keep the same city and state, which is what makes ZIP -> CITY functional.
ZIP_ANCHORS: list[tuple[str, str, int]] = [
    ("New York", "NY", 10001), ("Brooklyn", "NY", 11201), ("Buffalo", "NY", 14201),
    ("Rochester", "NY", 14604), ("Albany", "NY", 12201), ("Syracuse", "NY", 13202),
    ("Los Angeles", "CA", 90001), ("San Diego", "CA", 92101), ("San Jose", "CA", 95101),
    ("San Francisco", "CA", 94102), ("Sacramento", "CA", 95814), ("Fresno", "CA", 93701),
    ("Long Beach", "CA", 90802), ("Oakland", "CA", 94601), ("Bakersfield", "CA", 93301),
    ("Chicago", "IL", 60601), ("Aurora", "IL", 60502), ("Naperville", "IL", 60540),
    ("Springfield", "IL", 62701), ("Peoria", "IL", 61602),
    ("Houston", "TX", 77002), ("San Antonio", "TX", 78201), ("Dallas", "TX", 75201),
    ("Austin", "TX", 78701), ("Fort Worth", "TX", 76102), ("El Paso", "TX", 79901),
    ("Arlington", "TX", 76010), ("Corpus Christi", "TX", 78401),
    ("Phoenix", "AZ", 85001), ("Tucson", "AZ", 85701), ("Mesa", "AZ", 85201),
    ("Chandler", "AZ", 85224), ("Scottsdale", "AZ", 85251),
    ("Philadelphia", "PA", 19102), ("Pittsburgh", "PA", 15201), ("Allentown", "PA", 18101),
    ("Erie", "PA", 16501), ("Harrisburg", "PA", 17101),
    ("Jacksonville", "FL", 32202), ("Miami", "FL", 33101), ("Tampa", "FL", 33602),
    ("Orlando", "FL", 32801), ("Saint Petersburg", "FL", 33701), ("Tallahassee", "FL", 32301),
    ("Columbus", "OH", 43201), ("Cleveland", "OH", 44101), ("Cincinnati", "OH", 45202),
    ("Toledo", "OH", 43604), ("Akron", "OH", 44301), ("Dayton", "OH", 45402),
    ("Charlotte", "NC", 28202), ("Raleigh", "NC", 27601), ("Greensboro", "NC", 27401),
    ("Durham", "NC", 27701), ("Winston Salem", "NC", 27101),
    ("Indianapolis", "IN", 46201), ("Fort Wayne", "IN", 46802), ("Evansville", "IN", 47708),
    ("South Bend", "IN", 46601),
    ("Seattle", "WA", 98101), ("Spokane", "WA", 99201), ("Tacoma", "WA", 98402),
    ("Vancouver", "WA", 98660), ("Bellevue", "WA", 98004),
    ("Denver", "CO", 80202), ("Colorado Springs", "CO", 80903), ("Aurora", "CO", 80010),
    ("Fort Collins", "CO", 80521),
    ("Boston", "MA", 2108), ("Worcester", "MA", 1601), ("Springfield", "MA", 1103),
    ("Cambridge", "MA", 2138), ("Lowell", "MA", 1851),
    ("Detroit", "MI", 48201), ("Grand Rapids", "MI", 49503), ("Warren", "MI", 48089),
    ("Ann Arbor", "MI", 48104), ("Lansing", "MI", 48901),
    ("Nashville", "TN", 37201), ("Memphis", "TN", 38103), ("Knoxville", "TN", 37902),
    ("Chattanooga", "TN", 37402),
    ("Portland", "OR", 97201), ("Eugene", "OR", 97401), ("Salem", "OR", 97301),
    ("Las Vegas", "NV", 89101), ("Reno", "NV", 89501), ("Henderson", "NV", 89002),
    ("Louisville", "KY", 40202), ("Lexington", "KY", 40507),
    ("Milwaukee", "WI", 53202), ("Madison", "WI", 53703), ("Green Bay", "WI", 54301),
    ("Baltimore", "MD", 21201), ("Rockville", "MD", 20850), ("Frederick", "MD", 21701),
    ("Minneapolis", "MN", 55401), ("Saint Paul", "MN", 55101), ("Duluth", "MN", 55802),
    ("Kansas City", "MO", 64106), ("Saint Louis", "MO", 63101), ("Springfield", "MO", 65802),
    ("Atlanta", "GA", 30303), ("Augusta", "GA", 30901), ("Savannah", "GA", 31401),
    ("Columbus", "GA", 31901),
    ("Omaha", "NE", 68102), ("Lincoln", "NE", 68501),
    ("Oklahoma City", "OK", 73102), ("Tulsa", "OK", 74103),
    ("New Orleans", "LA", 70112), ("Baton Rouge", "LA", 70801), ("Shreveport", "LA", 71101),
    ("Richmond", "VA", 23219), ("Virginia Beach", "VA", 23450), ("Norfolk", "VA", 23510),
    ("Arlington", "VA", 22201),
    ("Newark", "NJ", 7102), ("Jersey City", "NJ", 7302), ("Paterson", "NJ", 7501),
    ("Trenton", "NJ", 8608),
    ("Hartford", "CT", 6103), ("New Haven", "CT", 6510), ("Bridgeport", "CT", 6604),
    ("Providence", "RI", 2903),
    ("Salt Lake City", "UT", 84101), ("Provo", "UT", 84601),
    ("Boise", "ID", 83702), ("Albuquerque", "NM", 87102), ("Little Rock", "AR", 72201),
    ("Birmingham", "AL", 35203), ("Montgomery", "AL", 36104), ("Jackson", "MS", 39201),
    ("Columbia", "SC", 29201), ("Charleston", "SC", 29401),
    ("Des Moines", "IA", 50309), ("Cedar Rapids", "IA", 52401),
    ("Wichita", "KS", 67202), ("Topeka", "KS", 66603), ("Charleston", "WV", 25301),
    ("Anchorage", "AK", 99501), ("Honolulu", "HI", 96813), ("Portland", "ME", 4101),
    ("Manchester", "NH", 3101), ("Burlington", "VT", 5401), ("Billings", "MT", 59101),
    ("Sioux Falls", "SD", 57104), ("Fargo", "ND", 58102), ("Cheyenne", "WY", 82001),
    ("Wilmington", "DE", 19801), ("Washington", "DC", 20001),
]

VARIANTS_PER_ANCHOR = 4


def zip_triples() -> list[tuple[str, str, str]]:
    """Return unique (zip, city, state) triples. ZIP determines city and state."""
    seen: dict[str, tuple[str, str]] = {}
    for city, state, base in ZIP_ANCHORS:
        for offset in range(VARIANTS_PER_ANCHOR):
            code = f"{base + offset:05d}"
            if len(code) == 5 and code not in seen:
                seen[code] = (city, state)
    return [(code, city, state) for code, (city, state) in sorted(seen.items())]


FIRST_NAMES = [
    "James", "Mary", "Robert", "Patricia", "John", "Jennifer", "Michael", "Linda",
    "David", "Elizabeth", "William", "Barbara", "Richard", "Susan", "Joseph", "Jessica",
    "Thomas", "Sarah", "Charles", "Karen", "Christopher", "Lisa", "Daniel", "Nancy",
    "Matthew", "Betty", "Anthony", "Margaret", "Mark", "Sandra", "Donald", "Ashley",
    "Steven", "Kimberly", "Andrew", "Emily", "Paul", "Donna", "Joshua", "Michelle",
    "Kenneth", "Carol", "Kevin", "Amanda", "Brian", "Dorothy", "George", "Melissa",
    "Timothy", "Deborah", "Ronald", "Stephanie", "Jason", "Rebecca", "Edward", "Sharon",
    "Jeffrey", "Laura", "Ryan", "Cynthia", "Jacob", "Kathleen", "Gary", "Amy",
    "Nicholas", "Angela", "Eric", "Shirley", "Jonathan", "Anna", "Stephen", "Brenda",
    "Larry", "Pamela", "Justin", "Emma", "Scott", "Nicole", "Brandon", "Helen",
]

LAST_NAMES = [
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis",
    "Rodriguez", "Martinez", "Hernandez", "Lopez", "Gonzalez", "Wilson", "Anderson",
    "Thomas", "Taylor", "Moore", "Jackson", "Martin", "Lee", "Perez", "Thompson",
    "White", "Harris", "Sanchez", "Clark", "Ramirez", "Lewis", "Robinson", "Walker",
    "Young", "Allen", "King", "Wright", "Scott", "Torres", "Nguyen", "Hill", "Flores",
    "Green", "Adams", "Nelson", "Baker", "Hall", "Rivera", "Campbell", "Mitchell",
    "Carter", "Roberts", "Gomez", "Phillips", "Evans", "Turner", "Diaz", "Parker",
    "Cruz", "Edwards", "Collins", "Reyes", "Stewart", "Morris", "Morales", "Murphy",
    "Cook", "Rogers", "Gutierrez", "Ortiz", "Morgan", "Cooper", "Peterson", "Bailey",
    "Reed", "Kelly", "Howard", "Ramos", "Kim", "Cox", "Ward", "Richardson",
]

# Provider specialties. Three-letter codes, the shape of a low-cardinality column.
SPECIALTIES = [
    "FAM", "INT", "CAR", "ORT", "DER", "NEU", "ONC", "PED",
    "PSY", "RAD", "SUR", "URO", "OBG", "END", "GAS", "OPH",
    "ANE", "EMR", "PUL", "DEN",
]

STATES = sorted({state for _, state, _ in ZIP_ANCHORS})

# Claim status codes: '02' dominates at roughly 70 percent.
CLAIM_STATUS_CODES = ["02", "01", "04", "11", "22", "99"]
CLAIM_STATUS_WEIGHTS = [0.70, 0.10, 0.07, 0.06, 0.04, 0.03]

# Member relationship codes, constrained by a CHECK on the table.
# FACETS relationship codes: the member themself, a dependent child, a spouse,
# a partner. EDI files carry the X12 codes (18, 19, 01, 53), translated on the
# way out.
RELATIONSHIP_CODES = ["M", "D", "S", "P"]
RELATIONSHIP_WEIGHTS = [0.44, 0.34, 0.20, 0.02]

# Service code leading letters. 'M' is reserved for medical products, which is
# how the "dental never carries an M service code" exclusion is planted.
MEDICAL_ONLY_LETTERS = ["M"]
SHARED_SERVICE_LETTERS = ["A", "B", "C", "D", "E", "G", "H", "J", "K", "L", "P", "R", "S", "T", "V"]

PLAN_NAME_PREFIXES = ["Choice", "Select", "Preferred", "Advantage", "Essential", "Premier",
                      "Value", "Secure", "Classic", "Total"]
PLAN_NAME_SUFFIXES = ["PPO", "HMO", "EPO", "POS", "HDHP"]


# Diagnosis codes, in the shape of ICD-10 without being any real code set. The
# claims analyzer ranks these, so the mix is deliberately uneven: a handful of
# common conditions carry most of the volume and a long tail carries the rest.
DIAGNOSES: list[tuple[str, str, str]] = [
    ("E119000", "Type 2 diabetes without complications", "Endocrine"),
    ("I10X000", "Essential hypertension", "Circulatory"),
    ("J069000", "Acute upper respiratory infection", "Respiratory"),
    ("M545000", "Low back pain", "Musculoskeletal"),
    ("F411000", "Generalized anxiety disorder", "Mental health"),
    ("E785000", "Disorder of lipid metabolism", "Endocrine"),
    ("J450000", "Asthma, uncomplicated", "Respiratory"),
    ("K219000", "Gastro-oesophageal reflux", "Digestive"),
    ("M179000", "Osteoarthritis of knee", "Musculoskeletal"),
    ("F329000", "Major depressive episode", "Mental health"),
    ("N390000", "Urinary tract infection", "Genitourinary"),
    ("R519000", "Headache", "Symptoms"),
    ("I251000", "Atherosclerotic heart disease", "Circulatory"),
    ("E668000", "Obesity", "Endocrine"),
    ("G470000", "Insomnia", "Nervous system"),
    ("L309000", "Dermatitis, unspecified", "Skin"),
    ("H520000", "Hypermetropia", "Eye"),
    ("K029000", "Dental caries", "Digestive"),
    ("K051000", "Chronic gingivitis", "Digestive"),
    ("Z010000", "Routine examination", "Factors"),
    ("J029000", "Acute pharyngitis", "Respiratory"),
    ("M255000", "Joint pain", "Musculoskeletal"),
    ("R109000", "Abdominal pain", "Symptoms"),
    ("N189000", "Chronic kidney disease", "Genitourinary"),
    ("I489000", "Atrial fibrillation", "Circulatory"),
    ("E039000", "Hypothyroidism", "Endocrine"),
    ("B349000", "Viral infection, unspecified", "Infectious"),
    ("S934000", "Sprain of ankle", "Injury"),
    ("H669000", "Otitis media", "Ear"),
    ("D649000", "Anaemia, unspecified", "Blood"),
]

# A few codes carry most of the volume, as they do in a real book.
DIAGNOSIS_WEIGHTS = [
    0.11, 0.10, 0.08, 0.07, 0.06, 0.055, 0.05, 0.045, 0.04, 0.038,
    0.034, 0.030, 0.027, 0.025, 0.022, 0.020, 0.018, 0.016, 0.015, 0.014,
    0.012, 0.011, 0.010, 0.009, 0.008, 0.007, 0.006, 0.005, 0.004, 0.003,
]

# Dental claims draw from the two dental diagnoses only.
DENTAL_DIAGNOSES = ["K029000", "K051000"]

CLAIM_TYPES = ["P", "I", "D"]
