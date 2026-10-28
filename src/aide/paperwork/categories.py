"""The folders of your documents in the panel.

Housing, Work, Education, Insurance, Car, Health and Other. The code decides them, at no
cost: first the type the model read and, when that is not enough (an insurance, a
contract, “other”), words from the title, the entity, the summary and the file name. Car
insurance goes to Car and medical insurance to Health; the town hall registration, to
Housing.
"""

from __future__ import annotations

from typing import Any

from aide.common.text import normalize

# Key, name, icon and color of each folder, in the order they are shown.
CATEGORIES = (
    ("housing", "Housing", "today", "pink"),
    ("work", "Work", "work", "green"),
    ("education", "Education", "education", "blue"),
    ("insurance", "Insurance", "shield", "lilac"),
    ("car", "Car", "car", "butter"),
    ("health", "Health", "health", "turquoise"),
    ("other", "Other", "documents", "gray"),
)
KIND_TO_CATEGORY = {
    "housing": "housing",
    "municipality": "housing",
    "work": "work",
    "education": "education",
    "car": "car",
    "health": "health",
}
# Words already normalized (lowercase and without accents): English first, then Spanish, German and
# French. With “*” at the end any word starting that way matches; without it, only the whole word
# (“auto” is not “authorization”, nor “master”, “Mastercard”). No company or country-specific agency
# names: each person adds their own, if they want, in their copy.
WORDS = {
    "housing": "apartment* rent rents rental* lease* leasing tenan* landlord* sublet* "
    "sublease* housing house home utilit* electricity "
    "piso* alquiler* subalquiler* vivienda* inventario* llaves empadronamiento* fianza* "
    "arrendador* arrendamiento* electricidad "
    "miet* untermiet* wohnung* anmeldung* einwohner* kaution* nebenkosten* vermieter* "
    "hausverwaltung* strom* "
    "loyer* logement* appartement* locataire*",
    "work": "work job jobs employ* payroll* payslip* paycheck* salary* wage wages timesheet* "
    "trabajo* laboral* empleador* empleo* sueldo* salario* nomina* horario* "
    "arbeit* lohn* gehalt* "
    "travail* emploi* salaire* employeur*",
    "car": "car cars vehicle* driver* driving motor* automobile* parking* "
    "coche* vehicul* matricula* conducir aparcamiento* "
    "auto autos autoversicherung* automobil* fahrzeug* kfz fuhrerschein* fuehrerschein* "
    "strassenverkehr* parkplatz* "
    "voiture* stationnement*",
    "health": "health medic* hospital* clinic* doctor doctors dentist* dental pharmac* "
    "prescription* patient* "
    "salud sanitari* clinica* "
    "kranken* arzt* zahnarzt* mediz* "
    "sante maladie* medecin*",
    "education": "degree* diplom* transcript* universi* thesis bachelor* master masters "
    "doctorate* phd school* college* "
    "titulo* expediente* tesis "
    "zeugnis* studium* hochschul* "
    "etudes",
    "insurance": "insurance* insurer* insured policy "
    "seguro* poliza* "
    "versicherung* "
    "assurance* assicuraz*",
}
# Outside the type, the order in which they are tried: the specific (home, work, car, health) before
# the general, so that a “medical record” goes to Health and not to Education.
ORDER = ("housing", "work", "car", "health", "education", "insurance")


def _has(words: list[str], category: str) -> bool:
    for key in WORDS[category].split():
        if key.endswith("*"):
            if any(p.startswith(key[:-1]) for p in words):
                return True
        elif key in words:
            return True
    return False


def category(d: dict[str, Any]) -> str:
    """The folder of a saved document."""
    kind = d.get("kind") or "other"
    if kind in KIND_TO_CATEGORY:
        return KIND_TO_CATEGORY[kind]
    text = " ".join(str(d.get(c) or "") for c in ("title", "entity", "summary", "original_name"))
    words = normalize(text).split()
    if kind == "insurance":
        return next((c for c in ("car", "health") if _has(words, c)), "insurance")
    return next((c for c in ORDER if _has(words, c)), "other")
