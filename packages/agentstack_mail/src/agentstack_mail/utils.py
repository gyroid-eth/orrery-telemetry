"""Utility helpers for the MCP Agent Mail service."""
# Derived from the frozen MCP Agent Mail live baseline.
# See NOTICE.md, UPSTREAM_LICENSE, and AGENTSTACK_LICENSE for provenance and terms.

from __future__ import annotations

import random
import re
from typing import Iterable, Optional

# Agent name word lists - used to generate memorable adjective+noun combinations
# These lists are designed to provide a large namespace while keeping names
# easy to remember, spell, and distinguish.
#
# Design principles:
# - All words are capitalized for consistent CamelCase output (e.g., "GreenLake")
# - Adjectives are colors, weather, materials, and nature-themed descriptors
# - Nouns are nature, geography, animals, simple objects, and historical scientists
# - No offensive, controversial, or confusing words
# - No words that could be easily misspelled or confused with each other

ADJECTIVES: Iterable[str] = (
    # Colors (original + expanded)
    "Red",
    "Orange",
    "Pink",
    "Black",
    "Purple",
    "Blue",
    "Brown",
    "White",
    "Green",
    "Chartreuse",
    "Lilac",
    "Fuchsia",
    "Azure",
    "Amber",
    "Coral",
    "Crimson",
    "Cyan",
    "Gold",
    "Gray",
    "Indigo",
    "Ivory",
    "Jade",
    "Lavender",
    "Magenta",
    "Maroon",
    "Navy",
    "Olive",
    "Pearl",
    "Rose",
    "Ruby",
    "Sage",
    "Scarlet",
    "Silver",
    "Teal",
    "Topaz",
    "Violet",
    "Cobalt",
    "Copper",
    "Bronze",
    "Emerald",
    "Sapphire",
    "Turquoise",
    # Weather and nature
    "Sunny",
    "Misty",
    "Foggy",
    "Stormy",
    "Windy",
    "Frosty",
    "Dusty",
    "Hazy",
    "Cloudy",
    "Rainy",
    # Descriptive
    "Swift",
    "Quiet",
    "Bold",
    "Bald",
    "Calm",
    "Bright",
    "Dark",
    "Wild",
    "Silent",
    "Gentle",
    "Rustic",
    # Additional descriptors (for scientist-name combinations)
    "Sharp",
    "Keen",
    "Vivid",
    "Brave",
    "Noble",
    "Sturdy",
    "Curious",
    # Expanded simple adjectives (2026-06-26) — widen the auto-generated
    # namespace. SIMPLE_ADJECTIVES x SCIENTIST_NOUNS had saturated at 899 names
    # (898/899 taken; collision checks count every agent ever registered, incl.
    # retired, so the namespace is consumed permanently). These also feed
    # _VALID_AGENT_NAMES so generated names stay valid.
    "Lucky",
    "Happy",
    "Merry",
    "Jolly",
    "Lively",
    "Clever",
    "Nimble",
    "Hardy",
    "Mighty",
    "Sleek",
    "Cozy",
    "Grand",
    "Royal",
    "Loyal",
    "Proud",
    "Humble",
    "Eager",
    "Warm",
    "Cool",
    "Fresh",
    "Crisp",
    "Pure",
    "Kind",
    "Quick",
    "Wise",
    "Witty",
    "Spry",
    "Brisk",
    "Steady",
    "Mellow",
    "Agile",
    "Trusty",
    "Breezy",
    "Snowy",
    "Starry",
    "Wintry",
    "Sandy",
    "Rocky",
    "Leafy",
    "Mossy",
    # Further simple adjectives (2026-06-26, round 2) — also feed
    # _VALID_AGENT_NAMES; kept in sync with SIMPLE_ADJECTIVES.
    "Tan",
    "Mint",
    "Lime",
    "Aqua",
    "Slate",
    "Ash",
    "Hazel",
    "Cream",
    "Peach",
    "Steel",
    "Brass",
    "Plum",
    "Cherry",
    "Cocoa",
    "Icy",
    "Balmy",
    "Chilly",
    "Gusty",
    "Dewy",
    "Polar",
    "Solar",
    "Lunar",
    "Cosmic",
    "Stellar",
    "Jovial",
    "Cheery",
    "Plucky",
    "Deft",
    "Astute",
    "Dapper",
    "Neat",
    "Smart",
    "Hearty",
    "Snug",
    "Zesty",
    "Jaunty",
    "Rosy",
    "Lush",
)

NOUNS: Iterable[str] = (
    # Original nouns
    "Stone",
    "Lake",
    "Dog",
    "Creek",
    "Pond",
    "Cat",
    "Bear",
    "Mountain",
    "Hill",
    "Snow",
    "Castle",
    # Geography and nature
    "River",
    "Forest",
    "Valley",
    "Canyon",
    "Meadow",
    "Prairie",
    "Desert",
    "Island",
    "Cliff",
    "Cave",
    "Glacier",
    "Waterfall",
    "Spring",
    "Stream",
    "Reef",
    "Dune",
    "Ridge",
    "Peak",
    "Gorge",
    "Marsh",
    "Brook",
    "Glen",
    "Grove",
    "Hollow",
    "Basin",
    "Cove",
    "Bay",
    "Harbor",
    # Animals
    "Fox",
    "Wolf",
    "Hawk",
    "Eagle",
    "Owl",
    "Deer",
    "Elk",
    "Moose",
    "Falcon",
    "Raven",
    "Heron",
    "Crane",
    "Otter",
    "Beaver",
    "Badger",
    "Finch",
    "Robin",
    "Sparrow",
    "Lynx",
    "Puma",
    # Objects and structures
    "Tower",
    "Bridge",
    "Forge",
    "Mill",
    "Barn",
    "Gate",
    "Anchor",
    "Lantern",
    "Beacon",
    "Compass",
    # Scientists (historical figures)
    "Pascal",
    "Hooke",
    "Einstein",
    "Feynman",
    "Darwin",
    "Tesla",
    "Gauss",
    "Euler",
    "Bohr",
    "Curie",
    "Faraday",
    "Planck",
    "Newton",
    "Pasteur",
    "Linnaeus",
    "Arrhenius",
    "Langmuir",
    "Boltzmann",
    "Ostwald",
    "Vesalius",
    "Guericke",
    "Fabre",
    "Leeuwenhoek",
    "Turing",
    "Mendel",
    "Kepler",
    "Maxwell",
    "Ramanujan",
    "Hubble",
    # Added 2026-06-26 (round 2) — keep in sync with SCIENTIST_NOUNS below.
    "Galileo",
    "Copernicus",
    "Archimedes",
    "Mendeleev",
    "Fermi",
    "Dirac",
    "Franklin",
    "Edison",
    "Koch",
    "Bell",
    # Added 2026-06-26 (round 3) — keep in sync with SCIENTIST_NOUNS below.
    "Yukawa",
    "Lovelace",
    "Noether",
    "Somerville",
    "Pauling",
    "Watt",
    "Hopper",
    "Lavoisier",
    "Bose",
    "Lamarr",
    "Goodall",
)

# Simple adjectives — the adjective half of auto-generated "adjective+scientist"
# names (generate_agent_name()). Expanded 2026-06-26: the previous 31-adjective
# list x 29 scientists = 899 names had saturated (898/899 taken). Collision checks
# count every agent ever registered (incl. retired), so the namespace is consumed
# permanently and a too-small pool eventually fails with "Unable to generate a
# unique agent name". Round 3 (2026-06-26) has 134 adjectives; with 50
# scientists that is 6700 names. Every entry here is also in ADJECTIVES so
# validate_agent_name_format() recognizes the output.
SIMPLE_ADJECTIVES: Iterable[str] = (
    # Basic colors
    "Red", "Orange", "Pink", "Black", "Purple", "Blue", "Brown", "White",
    "Green", "Gold", "Gray", "Navy", "Silver",
    # Extended colors (still simple and memorable)
    "Amber", "Coral", "Crimson", "Cyan", "Indigo", "Jade", "Olive", "Rose",
    "Ruby", "Sage", "Scarlet", "Teal", "Violet", "Copper", "Bronze", "Emerald",
    "Azure",
    # Weather and nature
    "Sunny", "Foggy", "Stormy", "Windy", "Frosty", "Cloudy", "Rainy", "Misty",
    "Hazy", "Dusty", "Breezy", "Snowy", "Starry", "Wintry", "Sandy", "Rocky",
    "Leafy", "Mossy",
    # Descriptive
    "Swift", "Quiet", "Bold", "Calm", "Bright", "Dark", "Wild", "Brave",
    "Noble", "Curious", "Sharp", "Gentle", "Silent", "Keen", "Vivid", "Sturdy",
    "Lucky", "Happy", "Merry", "Jolly", "Lively", "Clever", "Nimble", "Hardy",
    "Mighty", "Sleek", "Cozy", "Grand", "Royal", "Loyal", "Proud", "Humble",
    "Eager", "Warm", "Cool", "Fresh", "Crisp", "Pure", "Kind", "Quick",
    "Wise", "Witty", "Spry", "Brisk", "Steady", "Mellow", "Agile", "Trusty",
    # Round 2 (2026-06-26) — extended shades, celestial/weather, descriptors
    "Tan", "Mint", "Lime", "Aqua", "Slate", "Ash", "Hazel", "Cream", "Peach",
    "Steel", "Brass", "Plum", "Cherry", "Cocoa",
    "Icy", "Balmy", "Chilly", "Gusty", "Dewy", "Polar", "Solar", "Lunar",
    "Cosmic", "Stellar",
    "Jovial", "Cheery", "Plucky", "Deft", "Astute", "Dapper", "Neat", "Smart",
    "Hearty", "Snug", "Zesty", "Jaunty", "Rosy", "Lush",
)

# Scientist names only — used by generate_agent_name() so auto-generated names
# always follow the adjective+scientist pattern (e.g. "SwiftBohr", "BlueCurie").
# NOUNS is kept intact for validate_agent_name_format() backward compatibility.
SCIENTIST_NOUNS: Iterable[str] = (
    "Pascal",
    "Hooke",
    "Einstein",
    "Feynman",
    "Darwin",
    "Tesla",
    "Gauss",
    "Euler",
    "Bohr",
    "Curie",
    "Faraday",
    "Planck",
    "Newton",
    "Pasteur",
    "Linnaeus",
    "Arrhenius",
    "Langmuir",
    "Boltzmann",
    "Ostwald",
    "Vesalius",
    "Guericke",
    "Fabre",
    "Leeuwenhoek",
    "Turing",
    "Mendel",
    "Kepler",
    "Maxwell",
    "Ramanujan",
    "Hubble",
    # Added 2026-06-26 (round 2). Each has a dashboard portrait
    # (portraits_64/<Name>.png + portraits_pixel/<Name>.png) and an entry in
    # the index.html SCIENTISTS array + MURMURS.
    "Galileo",
    "Copernicus",
    "Archimedes",
    "Mendeleev",
    "Fermi",
    "Dirac",
    "Franklin",
    "Edison",
    "Koch",
    "Bell",
    # Added 2026-06-26 (round 3). Each has a dashboard portrait
    # (portraits_64/<Name>.png + portraits_pixel/<Name>.png) and an entry in
    # the index.html SCIENTISTS array + MURMURS.
    "Yukawa",
    "Lovelace",
    "Noether",
    "Somerville",
    "Pauling",
    "Watt",
    "Hopper",
    "Lavoisier",
    "Bose",
    "Lamarr",
    "Goodall",
)

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_AGENT_NAME_RE = re.compile(r"[^A-Za-z0-9]+")
_THREAD_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

# Pre-built frozenset of all valid agent names (lowercase) for O(1) validation lookup.
# This is computed once at module load time rather than O(n*m) per validation call.
_VALID_AGENT_NAMES: frozenset[str] = frozenset(
    f"{adj}{noun}".lower() for adj in ADJECTIVES for noun in NOUNS
)


def slugify(value: str) -> str:
    """Normalize a human-readable value into a slug."""
    normalized = value.strip().lower()
    slug = _SLUG_RE.sub("-", normalized).strip("-")
    return slug or "project"


def generate_agent_name() -> str:
    """Return a random simple-adjective+scientist combination (e.g. 'SwiftBohr')."""
    adjective = random.choice(tuple(SIMPLE_ADJECTIVES))
    noun = random.choice(tuple(SCIENTIST_NOUNS))
    return f"{adjective}{noun}"


def validate_agent_name_format(name: str) -> bool:
    """
    Validate that an agent name matches the required adjective+noun format.

    CRITICAL: Agent names MUST be randomly generated two-word combinations
    like "GreenLake" or "BlueDog", NOT descriptive names like "BackendHarmonizer".

    Names should be:
    - Unique and easy to remember
    - NOT descriptive of the agent's role or task
    - One of the predefined adjective+noun combinations

    Note: This validation is case-insensitive to match the database behavior
    where "GreenLake", "greenlake", and "GREENLAKE" are treated as the same.

    Returns True if valid, False otherwise.
    """
    if not name:
        return False

    # O(1) lookup using pre-built frozenset (vs O(n*m) iteration)
    return name.lower() in _VALID_AGENT_NAMES


def sanitize_agent_name(value: str) -> Optional[str]:
    """Normalize user-provided agent name; return None if nothing remains."""
    cleaned = _AGENT_NAME_RE.sub("", value.strip())
    if not cleaned:
        return None
    return cleaned[:128]


def validate_thread_id_format(thread_id: str) -> bool:
    """Validate that a thread_id is safe for filenames and indexing.

    Thread IDs are used as human-facing keys and may also be used in filesystem
    paths for thread digests. For safety and portability, enforce:
    - ASCII alphanumerics plus '.', '_', '-'
    - Must start with an alphanumeric character
    - Max length 128
    """
    candidate = (thread_id or "").strip()
    if not candidate:
        return False
    return _THREAD_ID_RE.fullmatch(candidate) is not None
