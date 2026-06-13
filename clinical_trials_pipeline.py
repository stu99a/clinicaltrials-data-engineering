import requests
import pandas as pd
import re
import time
import math
import unicodedata

from pymongo import MongoClient, UpdateOne
from pymongo.errors import BulkWriteError
import os


""" === TrialFetcher Class ==="""
# Handles querying the ClinicalTrials.gov API and fetching raw study data
class TrialFetcher:
    def __init__(self, expr="Neurofibromatosis", status="ACTIVE_NOT_RECRUITING"):
        self.base_url = "https://clinicaltrials.gov/api/v2/studies"
        self.expr = expr
        self.status = status

    def fetch_all(self):
        """
        Fetches all studies that match the query and status, 
        handling pagination with pageToken.
        """
        results = []
        page_token = None
        while True:
            # Build query parameters
            params = {
                "query.term": self.expr,
                "filter.overallStatus": self.status,
                "pageSize": 100
            }
            if page_token: 
                params["pageToken"] = page_token

            # Send request to API
            try:
                r = requests.get(self.base_url, params=params)
                r.raise_for_status()
                data = r.json()
            except Exception as e:
                print("Request failed:", e)
                break

            # Extract study records
            studies = data.get("studies", [])
            results.extend(studies)

            # Check if more pages exist
            page_token = data.get("nextPageToken")
            if not page_token:
                break

            # Sleep to avoid hitting rate limits
            time.sleep(0.3)

        return results
    
    
class MongoWriter:
    """
    Writes parsed ClinicalTrials.gov records into MongoDB Atlas
    (viewable in MongoDB Compass).
    """
    def __init__(self, uri, db_name="clinical_trials", collection_name="nf_eligibility_parsed"):
        self.client = MongoClient(uri)
        self.db = self.client[db_name]
        self.collection = self.db[collection_name]

        # Prevent duplicate trials
        
        # Index creation disabled (handled manually in Atlas)

    def upsert_trials(self, records):
        ops = []
        for rec in records:
            if not rec.get("Nct_id"):
                continue

            ops.append(
                UpdateOne(
                    {"Nct_id": rec["Nct_id"]},
                    {"$set": rec},
                    upsert=True
                )
            )


        if not ops:
            print("No MongoDB records to write.")
            return

        try:
            self.collection.bulk_write(ops, ordered=False)
            print(f"✔ Upserted {len(ops)} trials into MongoDB")
        except BulkWriteError as e:
            print("MongoDB bulk write error:", e.details)



"""=== TrialParser Class ==="""
# Parses a single study into a structured dictionary of fields
class TrialParser:
    def __init__(self, study):
        self.study = study
        self.parsed = {}

    def parse(self):
       # Master method to run all parsing steps on a study. Returns a dictionary of extracted fields.
        self._basic_info()
        self._principal_investigator()
        self._age()
        self._gender()
        self._eligibility()
        self._pregnancy()
        self._race()
        self._conditions()
        self._family()
        self._medications()
        self._drugs()
        self._surgery()
        self._comorbidities()
        return self.parsed

    def _basic_info(self):
        # Extracts identifiers, title, status, sponsor institution, and builds a ClinicalTrials.gov study URL.
        prot = self.study.get("protocolSection", {})
        ids = prot.get("identificationModule", {})
        status = prot.get("statusModule", {})
        sponsor = prot.get("sponsorCollaboratorsModule", {})
        nct_id = ids.get("nctId")

        self.parsed.update({
            "Nct_id": ids.get("nctId"),
            "Title": ids.get("briefTitle"),
            "Status": status.get("overallStatus"),
            "Institution": sponsor.get("leadSponsor", {}).get("name"),
            "Url": f"https://clinicaltrials.gov/study/{nct_id}" if nct_id else None
        })

    def _principal_investigator(self):
        #Extracts the first listed Principal Investigator (PI), or returns NA if none available.
        contacts = self.study.get("protocolSection", {}).get("contactsLocationsModule", {})
        officials = contacts.get("overallOfficials", [])
        
        if officials:
            first = officials[0]
            name = first.get("name", "").strip()
            affiliation = first.get("affiliation", "").strip()
            
            if name and affiliation:
                pi = f"{name}, {affiliation}"
            elif name:
                pi = name
            elif affiliation:
                pi = affiliation
            else:
                pi = "NA"
        else:
            pi = ""
        
        self.parsed["Principal_investigator"] = pi


    def _age(self):
        # Extracts min/max age and creates a simplified, numeric age range string.
        # Converts all units to years and formats like: >18, 1–30, ≤21, or N/A
        # Rounds up to the nearest whole year.
        
        elig = self.study.get("protocolSection", {}).get("eligibilityModule", {})
        min_age = elig.get("minimumAge")
        max_age = elig.get("maximumAge")
    
        def to_years(age_str):
            if not age_str:
                return None
            age_str = age_str.lower().strip()
            match = re.search(r"(\d+)", age_str)
            if not match:
                return None
            num = int(match.group(1))
            if "year" in age_str:
                return num
            if "month" in age_str:
                return math.ceil(num / 12)
            if "week" in age_str:
                return math.ceil(num / 52)
            if "day" in age_str:
                return math.ceil(num / 365)
            return None
    
        min_val = to_years(min_age)
        max_val = to_years(max_age)
    
        # Build simplified label
        if min_val is None and max_val is None:
            age_range = ""
        elif min_val is not None and max_val is not None:
            age_range = f"{min_val}–{max_val}"
        elif min_val is not None:
            age_range = f"≥{min_val}"
        else:
            age_range = f"≤{max_val}"
    
        self.parsed.update({
            "Min_age": min_val,
            "Max_age": max_val,
            "Age_range": age_range,
        })


    def _gender(self):
        # Extracts sex eligibility and maps it into binary columns (Male/Female), while preserving the raw value returned by API.
        elig = self.study.get("protocolSection", {}).get("eligibilityModule", {})
        gender_raw = elig.get("sex")
        self.parsed["Gender_raw"] = gender_raw
    
        gender = (gender_raw or "").lower()
    
        if gender == "male":
            self.parsed.update({"Male": True, "Female": False, "Prefer_not_to_say": False})
        elif gender == "female":
            self.parsed.update({"Male": False, "Female": True, "Prefer_not_to_say": False})
        elif gender == "all":
            self.parsed.update({"Male": True, "Female": True, "Prefer_not_to_say": False})
        else:
            self.parsed.update({"Male": None, "Female": None, "Prefer_not_to_say": None})


    def _eligibility(self):
        # Extracts raw eligibility criteria text.
        elig = self.study.get("protocolSection", {}).get("eligibilityModule", {})
        text = (elig.get("eligibilityCriteria") or "").lower()
        self.parsed["eligibility_raw"] = text


    def _pregnancy(self):
        """
        Improved pregnancy detection logic.
        - Detects pregnancy mentions even in bullet points or standalone words.
        - Handles unicode variations, list items, and mixed formatting.
        - Accurately distinguishes exclusion vs inclusion.
        """

        txt = (self.parsed.get("eligibility_raw") or "").lower()

        # Normalize unicode & formatting
        txt = txt.replace("\u00a0", " ").replace("\t", " ").replace("\r", " ")

        # Split text into lines for bullet-point detection
        lines = [line.strip() for line in txt.split("\n") if line.strip()]

        # --- STEP 1: Detect if pregnancy is mentioned at all ---
        pregnancy_keywords = ["pregnan", "pregnancy", "pregnant", "pregnancies"]
        pregnancy_mentioned = any(
            any(pk in line for pk in pregnancy_keywords)
            for line in lines
        )

        if not pregnancy_mentioned:
            self.parsed.update({
                "Pregnancy_yes": '',
                "Pregnancy_no": '',
                "Pregnancy_reason": "no mention"
            })
            return

        # --- STEP 2: Detect explicit exclusion patterns ---
        exclusion_patterns = [
            r"not.*pregnan",
            r"exclude.*pregnan",
            r"prohibit.*pregnan",
            r"must not.*pregnan",
            r"non[- ]?pregnant",
            r"pregnant women (are )?excluded",
            r"pregnancy.*(excluded|not allowed|contraindicated)",
        ]

        # ALSO detect bullet-list or standalone exclusions like:
        # "* pregnancy"
        # "- pregnant"
        bullet_exclusion = any(
            re.fullmatch(r"[*\-•–]?\s*(pregnancy|pregnant)\s*", line)
            for line in lines
        )

        # Check if any exclusion pattern matches full text
        regex_exclusion = any(re.search(pat, txt) for pat in exclusion_patterns)

        if bullet_exclusion or regex_exclusion:
            self.parsed.update({
                "Pregnancy_yes": False,
                "Pregnancy_no": True,
                "Pregnancy_reason": "excluded"
            })
            return

        # --- STEP 3: If mentioned but no exclusion → included ---
        self.parsed.update({
            "Pregnancy_yes": True,
            "Pregnancy_no": False,
            "Pregnancy_reason": "included"
        })


    def _race(self):
        """
        Context-aware race and language extractor:
        - Finds short, human-related phrases (e.g., "chinese subjects", "asian patients", "participants of African American descent")
        - Captures languages mentioned as "<language>-speaking"
        - Avoids biochemical/substring false-positives (e.g., 'cytarabine', 'hamster', 'cell')
        - Dedupes and normalizes Race_reason, joined by commas
        - Sets race columns to "YES"/"NO" and sets Other correctly
        """
        def _expand_language_or_patterns(text):
            """
            Expands patterns like 'english- or spanish-speaking' into
            'english-speaking and spanish-speaking' for easier matching.
            """
            # english- or spanish-speaking -> english-speaking and spanish-speaking
            pattern1 = re.compile(r"\b([a-z]+)-\s*or\s*([a-z]+)-speaking\b", flags=re.I)
            text = pattern1.sub(lambda m: f"{m.group(1)}-speaking and {m.group(2)}-speaking", text)

            # english / spanish-speaking or english and spanish-speaking -> english-speaking and spanish-speaking
            pattern2 = re.compile(r"\b([a-z]+)\s*(?:/|and)\s*([a-z]+)-speaking\b", flags=re.I)
            text = pattern2.sub(lambda m: f"{m.group(1)}-speaking and {m.group(2)}-speaking", text)

            return text

        txt = (self.parsed.get("eligibility_raw") or "").lower()
        txt = _expand_language_or_patterns(txt)

        # Strict race tokens (words only)
        race_tokens = {
            "Asian": r"(asian|chinese|japanese|korean|filipino|vietnamese|thai|indian|pakistani|bangladeshi|indonesian|malaysian)",
            "American_Indian": r"(american indian|native american|alaska native)",
            "Black": r"(black|african american|afro[- ]?caribbean|nigerian|ghanaian|kenyan|ethiopian)",
            "Hispanic": r"(hispanic|latino|latina|latinx|mexican|puerto rican|cuban)",
            "Middle_Eastern": r"(middle eastern|arab(?!ine\b)|north african|mena|egyptian|lebanese|iranian|iraqi|syrian|moroccan|palestinian)",
            "White": r"(caucasian|\bwhite\b|european)",
            "Other": r"(other (race|ethnicity|ethnic|group|minority)|mixed race|multiracial|non[- ]?white|ethnic minority|pacific islander|native hawaiian|maori|aboriginal)"
        }

        # human nouns that indicate a participant/subject mention
        human_nouns = r"(participant|participants|subject|subjects|patient|patients|individual|individuals|person|people|volunteer|volunteers|cohort|population|speaking|group)"

        # exclusion context for lab/biochem words
        exclusion_context = re.compile(
            r"(hamster|mouse|rat|cell|cells|tissue|ovary|ovarian|line|model|xenograft|blood|lesion|patch|matter|membrane|plaque|protein|enzyme|gene|sample|cytarabine|arabinoside)",
            flags=re.I
        )

        captured = []          # raw matched short phrases (as found)
        matched_races = {k: False for k in race_tokens.keys() if k != "Other"}
        other_mentioned = False

        # --- Capture languages ---
        language_pattern = re.compile(r"\b([a-z]+)-speaking\b", flags=re.I)
        for lang_match in language_pattern.findall(txt):
            captured.append(f"{lang_match}-speaking")

        # Patterns we try for races
        for race, token in race_tokens.items():
            if race == "Other":
                other_pat = re.compile(rf"\b{token}\b", flags=re.I)
                if other_pat.search(txt):
                    if not re.search(r"\bother (criteria|disease|treatment|drug|agent|therapy|symptom|sign)\b", txt):
                        other_mentioned = True
                continue

            p1 = re.compile(rf"\b{token}\s+(?:{human_nouns})\b", flags=re.I)
            p2 = re.compile(rf"\b(?:{human_nouns})\s+(?:of|from)\s+{token}\b", flags=re.I)
            p3 = re.compile(rf"\b{token}\b(?:\W+\w+){{0,3}}?\W+(?:{human_nouns})\b|\b(?:{human_nouns})\b(?:\W+\w+){{0,3}}?\W+\b{token}\b",
                            flags=re.I)

            matches = []
            for p in (p1, p2, p3):
                for m in p.findall(txt):
                    if isinstance(m, tuple):
                        snippet = " ".join([part for part in m if part]).strip()
                    else:
                        snippet = m.strip()
                    if snippet:
                        matches.append(snippet)

            valid = [m for m in matches if not exclusion_context.search(m)]
            if valid:
                matched_races[race] = True
                for v in valid:
                    captured.append(re.sub(r"\s+", " ", v.lower()).strip())

        # Deduplicate
        seen = set()
        unique = []
        for phrase in captured:
            norm = phrase.strip().lower()
            if norm and norm not in seen:
                seen.add(norm)
                unique.append(phrase.strip())

        # Set Race_reason
        self.parsed["Race_reason"] = ", ".join(unique) if unique else "no mention"

        # Set race columns
        for race in matched_races:
            self.parsed[race] = "YES" if matched_races[race] else "NO"

        # Decide Other
        if self.parsed["Race_reason"] == "no mention":
            self.parsed["Other"] = "NO"
        else:
            if any(matched_races.values()):
                self.parsed["Other"] = "NO"
            else:
                self.parsed["Other"] = "YES" if other_mentioned else "NO"


    def _conditions(self):
        """
        Detects NF1, NF2, Schwannomatosis, and related conditions
        entirely from the *inclusion* portion of the free-text eligibility criteria
        (eligibility_raw). Behavior is same as before, except it scans ONLY the
        inclusion criteria.

        - Supports reversed word order ("type 2 neurofibromatosis")
        - Recognizes bracketed forms ([nf], [nf2], (nf), etc.)
        - Handles common typos (swannomatosis, schwanoma)
        - Normalizes spacing/unicode before regex
        - Sets Under_Investigation = True ONLY when investigational phrases
        are in proximity (±10 words) to an NF condition (not when about drugs)
        """

        def _normalize_text(s: str) -> str:
            if not s:
                return ""
            s = unicodedata.normalize("NFKD", s)
            s = s.replace("\u00a0", " ").replace("\r", " ").replace("\t", " ")
            # fix common mojibake if present
            s = s.replace("Ñ±", "\n").replace("â€¢", "•").replace("â€", '"').replace("â€™", "'")
            return s

        def _extract_inclusion_section(full_text: str):
            """
            Return the inclusion subsection (normalized lowercased text) and the
            heading text (original-cased) as a tuple: (section_text, heading_text).
            Returns ("", "") when no inclusion section is found.
            """
            if not full_text:
                return "", ""
            orig = full_text
            norm = _normalize_text(full_text)
            low = norm.lower()

            # match a heading line that contains 'inclusion' (first occurrence)
            heading_match = re.search(
                r"(^\s*(?:\d{0,2}\.\s*)?(?:key\s+|main\s+)?inclusion[s]?(?:\s*criteria)?\s*[:\-–—]?.*$)",
                orig, flags=re.IGNORECASE | re.MULTILINE
            )

            if heading_match:
                heading_text = heading_match.group(1).strip()
                heading_lower = heading_text.lower()
                # try to find the heading position in normalized lower text
                start_idx = low.find(heading_lower)
                if start_idx == -1:
                    # fallback to literal "inclusion criteria"
                    pos = low.find("inclusion criteria")
                    start_pos = (pos + len("inclusion criteria")) if pos != -1 else 0
                else:
                    start_pos = start_idx + len(heading_lower)
            else:
                # fallback to any literal 'inclusion' occurrence
                heading_text = "Inclusion Criteria"
                pos = low.find("inclusion criteria")
                if pos == -1:
                    pos = low.find("inclusion")
                    if pos == -1:
                        return "", ""
                    start_pos = pos + len("inclusion")
                else:
                    start_pos = pos + len("inclusion criteria")

            # stop at the next top-level heading (exclusion, eligibility, outcomes, etc.) or end
            next_heading_pattern = re.compile(
                r"(?m)^\s*(?:\d{0,2}\.\s*)?(?:exclusion[s]?(?:\s*criteria)?|eligibility\s*criteria|"
                r"ineligib(?:il)?it(?:y)?\s*criteria|randomizat(?:ion)?|outcome[s]?|withdrawal[s]?|"
                r"notes|references|safety|study objectives)\b",
                flags=re.IGNORECASE
            )

            tail = low[start_pos:]
            nm = next_heading_pattern.search(tail)
            section = tail[:nm.start()].strip() if nm else tail.strip()

            # tidy bullets / numbering
            section = re.sub(r"(^|\n)\s*[\-\*\u2022]+\s*", "\n", section)
            section = re.sub(r"(^|\n)\s*\d+[\.\)]\s*", "\n", section)
            return section.strip(), heading_text

        # ---- main ----
        txt_raw = (self.parsed.get("eligibility_raw") or "")
        # normalize & extract inclusion section
        inclusion_section, inclusion_heading = _extract_inclusion_section(txt_raw)

        # If no inclusion section found -> set all flags False/empty (same behavior as before)
        if not inclusion_section:
            self.parsed.update({
                "Neurofibromatosis Type 1": False,
                "Neurofibromatosis Type 2": False,
                "Schwannomatosis": False,
                "Under_Investigation": False,
                "conditions_source_text": ""
            })
            return

        txt = inclusion_section  # already lowercased in extractor

        # ---------- NF1 detection ----------
        nf1_pat = re.compile(
            r"(\bnf[\s-]?1\b"
            r"|neurofibromatosis[\s-]?type[\s-]?1"
            r"|type[\s-]?1 neurofibromatosis"
            r"|\[(?:nf1|nf)\]|\((?:nf1|nf)\)|\{(?:nf1|nf)\})",
            flags=re.I
        )
        nf1_match = nf1_pat.search(txt)

        # ---------- NF2 detection ----------
        nf2_pat = re.compile(
            r"(\bnf[\s-]?2\b"
            r"|neurofibromatosis[\s-]?type[\s-]?2"
            r"|type[\s-]?2 neurofibromatosis"
            r"|\[(?:nf2|nf)\]|\((?:nf2|nf)\)|\{(?:nf2|nf)\})",
            flags=re.I
        )
        nf2_match = nf2_pat.search(txt)

        # ---------- Schwannomatosis / Schwannoma detection ----------
        schw_pat = re.compile(r"(schwannomatosis|schwannoma|swannomatosis|schwanoma)", flags=re.I)
        schw_match = schw_pat.search(txt)

        # ---------- Under-investigation detection (context-sensitive) ----------
        cond_terms = (
            r"nf[\s-]?1|nf1|neurofibromatosis[\s-]?type[\s-]?1|type[\s-]?1 neurofibromatosis|"
            r"nf[\s-]?2|nf2|neurofibromatosis[\s-]?type[\s-]?2|type[\s-]?2 neurofibromatosis|"
            r"schwannomatosis|schwannoma|swannomatosis|schwanoma"
        )
        inv_terms = r"under investigation|investigational|being studied|currently studied|study of"

        under_inv_pat = re.compile(
            rf"(?:{cond_terms})(?:\W+\w+){{0,10}}(?:{inv_terms})"
            rf"|(?:{inv_terms})(?:\W+\w+){{0,10}}(?:{cond_terms})",
            flags=re.I
        )
        under_inv_match = under_inv_pat.search(txt)

        # ---------- Build conditions_source_text ----------
        matched_snippets = []
        if nf1_match:
            matched_snippets.append(nf1_match.group(0).strip())
        if nf2_match:
            matched_snippets.append(nf2_match.group(0).strip())
        if schw_match:
            matched_snippets.append(schw_match.group(0).strip())
        if under_inv_match:
            matched_snippets.append(under_inv_match.group(0).strip())

        source_text = ", ".join(matched_snippets) if matched_snippets else ""

        # ---------- Update parsed dictionary ----------
        self.parsed.update({
            "Neurofibromatosis Type 1": bool(nf1_match),
            "Neurofibromatosis Type 2": bool(nf2_match),
            "Schwannomatosis": bool(schw_match),
            "Under_Investigation": bool(under_inv_match),
            "conditions_source_text": source_text
        })



    def _family(self):
        """
        Detects mentions of family members in eligibility text,
        only if mentioned in relation to a medical condition.
        Extracts matching phrases into Family_source_text.
        """
        txt = (self.parsed.get("eligibility_raw") or "").lower()

        # Family and condition patterns
        family_terms = r"(parent[s]?|sibling[s]?|child(ren)?|relative|family member|cousin|aunt|uncle|grandparent)"
        condition_terms = r"(nf[\s-]?[12]|neurofibromatosis|schwannomatosis|tumou?r|disease|condition|diagnos|genetic)"

        # Pattern allowing up to 15 words between family and condition terms
        pattern = rf"(\b{family_terms}\b(?:\W+\w+){{0,15}}?\b{condition_terms}\b|\b{condition_terms}\b(?:\W+\w+){{0,15}}?\b{family_terms}\b)"

        # Find matches of family-condition contexts
        matches = re.findall(pattern, txt)

        # If matches are found, determine which family members are mentioned
        if matches:
            context_text = " ".join([m[0] if isinstance(m, tuple) else m for m in matches])

            parents = bool(re.search(r"\bparent(s)?\b", context_text))
            siblings = bool(re.search(r"\bsibling(s)?\b", context_text))
            children = bool(re.search(r"\bchild(ren)?\b", context_text))
            other = bool(re.search(r"\b(relative|family member|cousin|aunt|uncle|grandparent)\b", context_text))
            no_one = not (parents or siblings or children or other)

            # Deduplicate phrases
            seen = set()
            unique_phrases = [x.strip() for x in matches if x[0] not in seen and not seen.add(x[0])]
            self.parsed["Family_source_text"] = " | ".join(unique_phrases)

        else:
            # No family-condition context found
            parents = siblings = children = other = False
            no_one = True
            self.parsed["Family_source_text"] = ""

        # Update parsed fields
        self.parsed.update({
            "Parents": parents,
            "Siblings": siblings,
            "Children": children,
            "Other": other,
            "No_one_in_family": no_one
        })


    def _medications(self):
        """
        Detects if the eligibility text mentions participants being on medication,
        receiving treatment, or undergoing therapy. Adds 'Medication_source_text'
        to show the exact context phrase(s).
        """
        txt = (self.parsed.get("eligibility_raw") or "").lower()

        # Contextual medication-related patterns
        med_patterns = [
            r"receiving (?:any )?(?:drug|treatment|therapy|medication)",
            r"patients? (?:on|undergoing|receiving) (?:any )?(?:drug|therapy|treatment|medication)",
            r"use of (?:an|any) investigational (?:drug|therapy)",
            r"active (?:pharmaceutical|medical) therapy",
            r"currently (?:taking|receiving|under) (?:any )?(?:drug|treatment|therapy|medication)",
            r"any medication for treatment of",
            r"treated with",
            r"under (?:treatment|therapy)",
            r"receiving investigational (?:treatment|therapy)"
        ]

        found_phrases = []
        for pat in med_patterns:
            # Match full sentence fragment containing the keyword
            matches = re.findall(rf"([^.]*{pat}[^.]*)", txt)
            # Ensure all are strings, not tuples
            found_phrases.extend([m.strip() for m in matches if isinstance(m, str)])

        # Deduplicate
        seen = set()
        found_phrases = [f for f in found_phrases if not (f in seen or seen.add(f))]

        if found_phrases:
            self.parsed.update({
                "Medication_yes": True,
                "Medication_no": False,
                "Medication_source_text": ", ".join(found_phrases)
            })
        else:
            self.parsed.update({
                "Medication_yes": False,
                "Medication_no": True,
                "Medication_source_text": ""
            })


    def _drugs(self):
        """
        Detects known and unlisted drugs in eligibility criteria text.
        - Flags specific known drugs.
        - Captures all drug names (known and other) into 'Drug_source_text'.
        - 'Other_drug' = True if any drug outside the known list is found.
        """
    
        txt = (self.parsed.get("eligibility_raw") or "").lower()

        # --- Known drugs ---
        known_drugs = {
            "Selumetinib(kosulego)": r"\bselumetinib\b|\bkosulego\b",
            "Bevacizumab(avastin)": r"\bbevacizumab\b|\bavastin\b",
            "Everolimus(afinitor)": r"\beverolimus\b|\bafinitor\b",
            "Trametinib(mekinist)": r"\btrametinib\b|\bmekinist\b",
        }

        # --- Extended known list ---
        other_known_drugs = [
            "ipilimumab", "nivolumab", "sorafenib", "tipifarnib", "dabrafenib",
            "fluconazole", "binimetinib", "cobimetinib", "pembrolizumab", "mirdametinib",
            "cetuximab", "sirolimus", "temsirolimus", "brigatinib", "neratinib",
            "vemurafenib", "dolutegravir", "emtricitabine", "raltegravir", "tenofovir",
            "mitomycin", "cytarabine", "fludarabine", "tacrolimus", "carboplatin", "imatinib"
        ]

        # --- Generic drug name pattern ---
        generic_drug_pattern = r"\b[a-z]{3,}(mab|nib|limus|zole|platin|mycin|cillin|vir|trexate|xan|prost|gliflozin|gliptin|lukast|ciclovir|oxacin|olol|dipine)\b"

        found_known = set()
        found_other = set()

        # --- Match known drugs ---
        for drug, pat in known_drugs.items():
            if re.search(pat, txt):
                self.parsed[drug] = True
                found_known.add(drug.split("(")[0].lower())  # store the clean drug name
            else:
                self.parsed[drug] = False

        # --- Match extended known drugs ---
        for drug in other_known_drugs:
            if re.search(rf"\b{re.escape(drug)}\b", txt):
                found_other.add(drug.lower())

        # --- Dynamic generic drug detection ---
        dynamic_matches = re.findall(generic_drug_pattern, txt)
        if dynamic_matches:
            # Re-run with context capture to get full words, not just suffix
            word_matches = re.findall(r"\b[a-z]{3,}(?:mab|nib|limus|zole|platin|mycin|cillin|vir|trexate|xan|prost|gliflozin|gliptin|lukast|ciclovir|oxacin|olol|dipine)\b", txt)
            found_other.update(map(str.lower, word_matches))

        # --- Combine and deduplicate ---
        all_drugs = sorted(set(found_known) | found_other)

        # --- Flag other_drug if any not in known list ---
        other_drug_flag = any(d not in {k.split("(")[0].lower() for k in known_drugs} for d in found_other)

        # --- Update parsed dict ---
        self.parsed.update({
            "Other_drug": other_drug_flag,
            "Drug_source_text": ", ".join(all_drugs)
        })


    def _surgery(self):
        """
        Detects whether surgery or related procedures are mentioned in eligibility text.
        - Flags Surgery_yes / Surgery_no.
        - Returns only the matched surgical terms (not full sentences) in Surgery_source_text.
        """
        txt = (self.parsed.get("eligibility_raw") or "").lower()

        # Define all surgery-related terms
        surgery_terms = [
            "surgery", "surgical", "operation", "operative",
            "resection", "biopsy", "excision", "tumor removal",
            "debulking", "neurosurgery", "orthopedic surgery"
        ]

        # Match any of the keywords
        pattern = r"\b(" + "|".join(map(re.escape, surgery_terms)) + r")\b"
        matches = re.findall(pattern, txt)

        if matches:
            unique_terms = sorted(set(matches))
            self.parsed.update({
                "Surgery_yes": True,
                "Surgery_no": False,
                "Surgery_source_text": ", ".join(unique_terms)
            })
        else:
            self.parsed.update({
                "Surgery_yes": False,
                "Surgery_no": True,
                "Surgery_source_text": ""
            })


   
    def _comorbidities(self):
        """
        Whitelist-based comorbidity extraction (disease-only).
        Sets:
        - self.parsed["Diabetes"], ["Hypertension"], ["Asthma"] (booleans)
        - self.parsed["Other_comorbidity"] (boolean)
        - self.parsed["No_comorbidity"] (boolean)
        - self.parsed["Comorbidity_source_text"] like:
                "Exclusion Criteria: cancer, asthma, epilepsy"
        Only accepts comorbidities defined in WHITELIST (extendable).
        """

        def _normalize_text(s: str) -> str:
            if not s:
                return ""
            s = unicodedata.normalize("NFKD", s)
            s = s.replace("\r\n", "\n").replace("\r", "\n")
            # common mojibake fixes; add more if you see artifacts
            s = s.replace("Ñ±", "\n").replace("â€¢", "•").replace("â‰¥", ">=").replace("â€”", "-")
            s = s.replace("â€“", "-").replace("â€", '"').replace("â€™", "'")
            s = re.sub(r"[ \t]+", " ", s)
            return s

        def _extract_exclusion_section(full_text: str):
            if not full_text:
                return "", ""
            orig = full_text
            norm = _normalize_text(full_text)
            low = norm.lower()

            # find heading line containing 'exclusion'
            heading_match = re.search(
                r"(^\s*(?:\d{0,2}\.\s*)?(?:key\s+|main\s+)?exclusion[s]?(?:\s*criteria)?\s*[:\-–—]?.*$)",
                orig, flags=re.IGNORECASE | re.MULTILINE
            )
            if heading_match:
                heading_text = heading_match.group(1).strip()
                heading_lower = heading_text.lower()
                start_idx = low.find(heading_lower)
                start_pos = (start_idx + len(heading_lower)) if start_idx != -1 else low.find("exclusion criteria")
                start_pos = start_pos + len("exclusion criteria") if start_pos != -1 else 0
            else:
                heading_text = "Exclusion Criteria"
                pos = low.find("exclusion criteria")
                if pos == -1:
                    pos = low.find("exclusion")
                    if pos == -1:
                        return "", ""
                    start_pos = pos + len("exclusion")
                else:
                    start_pos = pos + len("exclusion criteria")

            # stop at next top-level heading or end
            next_heading_pattern = re.compile(
                r"(?m)^\s*(?:\d{0,2}\.\s*)?(?:inclusion[s]?(?:\s*criteria)?|eligibility\s*criteria|"
                r"ineligib(?:il)?it(?:y)?\s*criteria|randomizat(?:ion)?|outcome[s]?|withdrawal[s]?|"
                r"notes|references|safety|study objectives)\b",
                flags=re.IGNORECASE
            )
            tail = low[start_pos:]
            nm = next_heading_pattern.search(tail)
            section = tail[:nm.start()].strip() if nm else tail.strip()

            # tidy bullets / numbering
            section = re.sub(r"(^|\n)\s*[\-\*\u2022]+\s*", "\n", section)
            section = re.sub(r"(^|\n)\s*\d+[\.\)]\s*", "\n", section)
            return section.strip(), heading_text

        # ---- whitelist definition ----
        # Canonical label -> list of regex patterns (match ANY to include the label)
        WHITELIST = {
            "cancer / tumor": [r"\bmalignan(t|cy|cies)\b", r"\btumo?r\b", r"\bneoplasm\b"],
            "malignant peripheral nerve sheath tumor": [r"malignant peripheral nerve sheath", r"\bmpnst\b"],
            "optic glioma": [r"\boptic glioma\b"],
            "asthma": [r"\basthma\b"],
            "epilepsy / seizure disorder": [r"\bepilep\w*\b", r"\bseizure\b"],
            "interstitial pneumonia / pneumonitis": [r"\binterstitial pneumonia\b", r"\bpneumonitis\b"],
            "dysphagia": [r"\bdysphag\w*\b"],
            "malabsorption syndrome": [r"\bmalabsorp\w*\b"],
            "retinal vein occlusion": [r"\bretinal vein occlusion\b", r"\brvo\b"],
            "retinal pigment epithelial detachment (rped)": [r"\bretinal pigment epithelial\b", r"\brped\b"],
            "glaucoma": [r"\bglaucoma\b"],
            "heart failure / nyha >=3": [r"\bcongestive heart failure\b", r"\bnyha\b", r"\bheart failure\b"],
            "clinically significant arrhythmia": [r"\barrhythmia\b", r"\bcomplete left bundle branch block\b", r"\bav block\b"],
            "low LVEF (<50%)": [r"\blvef[^0-9]*<\s*50\b"],
            "recent thrombotic/embolic events": [r"\bthrombotic\b", r"\bembol\w*\b"],
            "stroke / cerebrovascular disease": [r"\bstroke\b", r"\bcerebrovascular\b"],
            "active hepatitis (B/C)": [r"\bhepatitis b\b", r"\bhep b\b", r"\bhepatitis c\b", r"\bhep c\b"],
            "HIV infection": [r"\bhiv\b", r"\bhuman immunodeficiency\b"],
            "thyroid / endocrine disorder": [r"\bthyroid\b", r"\bendocrine\b"],
            # add any further disease-only canonical labels here
        }

        # ---- main ----
        full_txt = (self.parsed.get("eligibility_raw") or "")
        if not full_txt or not full_txt.strip():
            self.parsed.update({
                "Diabetes": False,
                "Hypertension": False,
                "Asthma": False,
                "Other_comorbidity": False,
                "No_comorbidity": True,
                "Comorbidity_source_text": ""
            })
            return

        exclusion_section, exclusion_heading = _extract_exclusion_section(full_txt)
        if not exclusion_section:
            self.parsed.update({
                "Diabetes": False,
                "Hypertension": False,
                "Asthma": False,
                "Other_comorbidity": False,
                "No_comorbidity": True,
                "Comorbidity_source_text": ""
            })
            return

        # Lowercase working text
        work = exclusion_section.lower()

        # Collect matched canonical labels in discovery order
        matched = []
        for canonical_label, patterns in WHITELIST.items():
            for pat in patterns:
                if re.search(pat, work, flags=re.I):
                    # store lowercase canonical to keep deterministic small-caps style
                    lab = canonical_label.lower()
                    if lab not in matched:
                        matched.append(lab)
                    break  # stop on first pattern match for this canonical_label

        # Special explicit booleans: Diabetes, Hypertension, Asthma (still from the text if present)
        self.parsed["Diabetes"] = bool(re.search(r"\bdiabet(?:es|ic)\b", work, flags=re.I))
        self.parsed["Hypertension"] = bool(re.search(r"\bhypertension\b|\bhigh blood pressure\b|\buncontrolled hypertension\b", work, flags=re.I))
        # Asthma is already in whitelist; keep explicit flag too
        self.parsed["Asthma"] = bool(re.search(r"\basthma\b", work, flags=re.I))

        # If Diabetes/Hypertension/Asthma detected but not present in matched (because you may want them listed), add them
        if self.parsed["Diabetes"] and "diabetes" not in matched:
            matched.append("diabetes")
        if self.parsed["Hypertension"] and "hypertension" not in matched:
            matched.append("hypertension")
        if self.parsed["Asthma"] and "asthma" not in matched:
            matched.append("asthma")

        # Final dedupe preserving order
        seen = set()
        canonical_ordered = []
        for x in matched:
            k = x.strip()
            if k and k not in seen:
                seen.add(k)
                canonical_ordered.append(k)

        any_comorb = bool(canonical_ordered)
        # Other_comorbidity true if any matched label besides the three primary
        primary_set = {"diabetes", "hypertension", "asthma"}
        other_comorb = any([c for c in canonical_ordered if c not in primary_set])
        no_comorb = not any_comorb

        # Compose Comorbidity_source_text
        if any_comorb:
            prefix = exclusion_heading if exclusion_heading else "Exclusion Criteria"
            prefix = prefix.rstrip()
            if not prefix.endswith(":"):
                prefix = prefix + ":"
            # join using canonical_ordered (already lowercase canonical labels)
            source_text = f"{prefix} " + ", ".join(canonical_ordered)
        else:
            source_text = ""

        # Final update
        self.parsed.update({
            "Diabetes": self.parsed.get("Diabetes", False),
            "Hypertension": self.parsed.get("Hypertension", False),
            "Asthma": self.parsed.get("Asthma", False),
            "Other_comorbidity": bool(other_comorb),
            "No_comorbidity": bool(no_comorb),
            "Comorbidity_source_text": source_text
        })



# === Helper Function ===
# Converts boolean values to YES/NO/NA strings for cleaner CSV export
def bool_to_yesno(value):
    if pd.isna(value):
        return "NA"
    elif value is True:
        return "YES"
    elif value is False:
        return "NO"
    else:
        return value

# === Runner ===
if __name__ == "__main__":
    fetcher = TrialFetcher(expr="Neurofibromatosis", status="ACTIVE_NOT_RECRUITING")
    print("Fetching trials...")
    raw_trials = fetcher.fetch_all()
    print(f"Fetched {len(raw_trials)} studies...")

    print("Parsing trials...")
    parsed_trials = [TrialParser(study).parse() for study in raw_trials]
    df = pd.DataFrame(parsed_trials)

    # Convert only boolean columns to YES/NO/NA
    for col in df.columns:
        if df[col].dropna().isin([True, False]).any():
            df[col] = df[col].map(bool_to_yesno)

    # === CSV Output === 
    df.to_csv("clinical_trials_parsed.csv", index=False)
    print("Saved to clinical_trials_parsed.csv")

    # === MongoDB Output ===
    # Upserts the parsed clinical trials data into MongoDB 
    MONGO_URI = os.getenv("MONGO_URI")  # Ensure this is set in your environment
    mongo = MongoWriter(
        uri=MONGO_URI,
        db_name="iHealth_Dev",
        collection_name="Clinical Trial-Output"
    )

    mongo.upsert_trials(parsed_trials)

    print(df.head())
