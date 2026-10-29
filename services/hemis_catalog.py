"""Read, enrich, cache, and search the official HEMIS institution directory."""

import re
import time
from urllib.parse import urlsplit

import aiohttp


CATALOG_URL = "https://student.hemis.uz/rest/v1/public/university-api-urls"
CACHE_SECONDS = 3600
_cached_at = 0.0
_cached_institutions: list[dict[str, str]] = []

# Qo'shimcha va qayta nomlangan OTMlar (masalan, SamDAQU -> student.samdaqu.edu.uz)
SUPPLEMENTAL_INSTITUTIONS: list[dict[str, str]] = [
    {
        "code": "313_samdaqu",
        "name": "Mirzo Ulug'bek nomidagi Samarqand davlat arxitektura-qurilish universiteti (SamDAQU)",
        "api_url": "https://student.samdaqu.edu.uz/rest/v1/",
        "student_url": "https://student.samdaqu.edu.uz/",
    },
    {
        "code": "355_tdpu",
        "name": "Nizomiy nomidagi Toshkent davlat pedagogika universiteti (TDPU)",
        "api_url": "https://student.tdpu.uz/rest/v1/",
        "student_url": "https://student.tdpu.uz/",
    },
    {
        "code": "320_uzfi",
        "name": "O'zbekiston-Finlandiya pedagogika instituti (Samarqand davlat pedagogika instituti)",
        "api_url": "https://student.samdpi.uz/rest/v1/",
        "student_url": "https://student.samdpi.uz/",
    },
]

# Kod bo'yicha qidiruv kalit so'zlari va mashhur qisqartmalar (abbreviaturalar + eski nomlar)
INSTITUTION_ALIASES: dict[str, str] = {
    "313": "Samarqand davlat arxitektura-qurilish universiteti SamDAQU SamDAQI SamGASI SamDTU Mirzo Ulugbek arxitektura qurilish",
    "313_samdaqu": "Samarqand davlat arxitektura-qurilish universiteti SamDAQU SamDAQI SamGASI SamDTU Samarqand davlat texnika universiteti Mirzo Ulugbek arxitektura qurilish",
    "312": "SamDU Samarqand davlat universiteti Sharof Rashidov",
    "455": "SamDU Urgut filiali Samarqand davlat universiteti",
    "347": "SamDCHTI Samarqand davlat chet tillar instituti",
    "320": "SamDPI Ozbekiston Finlandiya pedagogika instituti OzFPI UzFI Samarqand pedagogika",
    "363": "SamDTU SamMI Samarqand davlat tibbiyot universiteti instituti",
    "372": "SamVMI SamDVMCHBU Samarqand veterinariya",
    "385": "SamISI SIES Samarqand iqtisodiyot va servis instituti",
    "477": "SIUT Samarqand xalqaro texnologiya universiteti",
    "388": "TATU Samarqand filiali TUIT SamTUIT",
    "360": "TDIU Samarqand filiali TSUE",
    "380": "TATU TUIT Toshkent axborot texnologiyalari universiteti aloqa Muhammad al Xorazmiy",
    "392": "TATU Fargona filiali TUIT",
    "391": "TATU Nukus filiali TUIT",
    "333": "TATU Nurafshon filiali TUIT",
    "389": "TATU Qarshi filiali TUIT",
    "390": "TATU Urganch filiali TUIT",
    "314": "OzMU UzMU NUU Mirzo Ulugbek nomidagi Ozbekiston Milliy universiteti Toshkent",
    "401": "OzMU Jizzax filiali UzMU NUU",
    "315": "TDTU Politex Toshkent davlat texnika universiteti Islom Karimov",
    "324": "TDIU TSUE Narxoz Toshkent davlat iqtisodiyot universiteti",
    "336": "TMI TFI Toshkent moliya instituti TDIU",
    "440": "Fiskal institut Soliq akademiyasi DSQ",
    "355": "TDPU Nizomiy Toshkent davlat pedagogika universiteti OzMPU",
    "328": "TAQU TAQI Toshkent arxitektura qurilish universiteti instituti TIACE",
    "384": "TDYU TSUL Yuridik Toshkent davlat yuridik universiteti",
    "364": "TTA TMA Toshkent tibbiyot akademiyasi",
    "547": "TDTU TTA Toshkent davlat tibbiyot universiteti",
    "367": "ToshPMI TashPMI Toshkent pediatriya tibbiyot instituti",
    "366": "TDSI TSDI Toshkent davlat stomatologiya instituti",
    "369": "FarMI Toshkent farmatsevtika instituti",
    "373": "TDAU Agrar Toshkent davlat agrar universiteti",
    "376": "TIQXMMI TIIAME Irrigatsiya Toshkent irrigatsiya",
    "334": "TDTrU TSTU Transport Toshkent davlat transport universiteti",
    "337": "TKTI TCTI Toshkent kimyo texnologiya instituti",
    "331": "TTYESI Tekstil Toshkent toqimachilik va yengil sanoat instituti",
    "326": "OzDJTU UzDJTU UzSWLU Jahon tillari Toshkent",
    "327": "TDSHU TSUOS Sharqshunoslik Toshkent davlat sharqshunoslik universiteti",
    "302": "TDOTAU Alisher Navoiy uzbek tili va adabiyoti Toshkent",
    "350": "JIDU UWED Jahon iqtisodiyoti va diplomatiya universiteti",
    "382": "OzDJTSU UzDJTSU Jismoniy tarbiya va sport Chirchiq",
    "354": "CHDPU CSPI Chirchiq davlat pedagogika universiteti instituti",
    "301": "ADU AndDU Andijon davlat universiteti",
    "361": "ADTI AndDTI Andijon davlat tibbiyot instituti",
    "303": "AndMI Andijon mashinasozlik instituti",
    "316": "ADPI AndDPI Andijon davlat pedagogika instituti",
    "446": "ADCHTI Andijon davlat chet tillari instituti",
    "371": "AndQXAI Andijon qishloq xojaligi",
    "304": "BuxDU Buxoro davlat universiteti",
    "362": "BuxDTI BSMI Abu Ali ibn Sino Buxoro davlat tibbiyot instituti",
    "305": "BMTI BuxMTI Buxoro muhandislik texnologiya instituti",
    "322": "BuxDPI Buxoro davlat pedagogika instituti",
    "539": "BDTU BSTU Buxoro davlat texnika universiteti",
    "342": "FarDU FDU Fargona davlat universiteti",
    "343": "FarPI FerPI Fargona politexnika instituti",
    "543": "FDTU FSTU Fargona davlat texnika universiteti",
    "344": "FJSTI Fargona jamoat salomatligi tibbiyot instituti",
    "356": "QDPI KSPI Qoqon davlat pedagogika instituti",
    "542": "QDU Qoqon davlat universiteti",
    "351": "JDPU Jizzax davlat pedagogika universiteti instituti",
    "308": "JizPI Jizzax politexnika instituti",
    "309": "NamDU Namangan davlat universiteti",
    "310": "NamMQI Namangan muhandislik qurilish instituti",
    "311": "NamMTI Namangan muhandislik texnologiya instituti",
    "541": "NamDTU Namangan davlat texnika universiteti",
    "450": "NamDCHTI Ishoqxon Ibrat Namangan chet tillari",
    "451": "NamDPI Namangan davlat pedagogika instituti",
    "319": "NDKTU NDKI Navoiy davlat konchilik va texnologiyalar universiteti",
    "353": "NavDU NDPI Navoiy davlat pedagogika instituti universiteti",
    "345": "QarDU Qarshi davlat universiteti",
    "375": "QMII QarMII Qarshi muhandislik iqtisodiyot instituti",
    "538": "QDTU KSTU Qarshi davlat texnika universiteti",
    "358": "ShDPI Shahrisabz davlat pedagogika instituti",
    "307": "GulDU Guliston davlat universiteti",
    "453": "GDPI Guliston davlat pedagogika instituti",
    "340": "TerDU Termiz davlat universiteti",
    "357": "TerDPI Termiz davlat pedagogika instituti",
    "379": "TerMTI Termiz muhandislik texnologiya instituti",
    "528": "TDMAU Termiz davlat muhandislik va agrotexnologiyalar universiteti",
    "341": "UrDU Abu Rayhon Beruniy Urganch davlat universiteti Xorazm",
    "452": "UrDPI Urganch davlat pedagogika instituti",
    "338": "UrDTI TTA Urganch filiali tibbiyot",
    "346": "QDU KarSU QQDU Berdaq Qoraqalpoq davlat universiteti Nukus",
    "352": "NDPI Ajiniyoz Nukus davlat pedagogika instituti",
    "349": "QQTI KKMI Qoraqalpogiston tibbiyot instituti Nukus",
    "540": "NDTU Nukus davlat texnika universiteti",
    "415": "KIUT Yeoju Toshkent Kimyo xalqaro universiteti",
    "421": "WIUT Vestminster Westminster Toshkent",
    "423": "INHA Inha Toshkent",
    "422": "MDIS MDIST Singapur Toshkent",
    "420": "TTPU Turin politexnika Toshkent",
    "429": "Webster Vebster Toshkent",
    "416": "CAU Akfa Central Asian University",
    "509": "ISFT International School of Finance",
    "457": "Alfraganus Toshkent",
    "506": "PDP University",
}

_CYRILLIC_TO_LATIN = {
    "а": "a", "б": "b", "в": "v", "г": "g", "ғ": "g", "д": "d",
    "е": "e", "ё": "yo", "ж": "j", "з": "z", "и": "i", "й": "y",
    "к": "k", "қ": "q", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ў": "o",
    "ф": "f", "х": "x", "ҳ": "h", "ц": "ts", "ч": "ch", "ш": "sh",
    "щ": "sh", "ъ": "", "ы": "i", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}

_HONORIFIC_RE = re.compile(
    r"^(?:[a-z0-9.\s]+\s+nomidagi\s+|nomli\s+)",
    re.IGNORECASE,
)


def _is_https_url(value: object) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlsplit(value)
    return parsed.scheme == "https" and bool(parsed.hostname)


def normalize_search_text(text: str, *, phonetic: bool = False) -> str:
    """Matnni qidiruv uchun bir xil (lotin, tutuq belgisisiz, tirelarsiz) ko'rinishga keltiradi."""
    lowered = (text or "").casefold()
    # Kirill harflarini lotinga o'girish
    chars = [_CYRILLIC_TO_LATIN.get(ch, ch) for ch in lowered]
    s = "".join(chars)
    # Barcha turdagi apostrof va tutuq belgilarini olib tashlash (o' -> o, g' -> g)
    for apo in ("'", "‘", "’", "ʻ", "ʼ", "`", "´", "ʹ", "ʽ", "ˈ"):
        s = s.replace(apo, "")
    # Tire va tinish belgilarini bo'sh joyga almashtirish
    s = re.sub(r"[^a-z0-9]+", " ", s)
    s = " ".join(s.split())
    if phonetic:
        # x/h va q/k farqlarini yumshatish (masalan: arhitektura == arxitektura)
        s = s.replace("h", "x").replace("q", "k")
    return s


def _strip_honorific(normalized_name: str) -> str:
    return _HONORIFIC_RE.sub("", normalized_name).strip()


def _domain_keywords(url: str) -> str:
    host = (urlsplit(url).hostname or "").casefold()
    parts = [p for p in re.split(r"[.-]+", host) if p not in ("student", "talaba", "hemis", "uz", "edu", "com", "org", "www")]
    return " ".join(parts)


def _institution_search_blob(item: dict[str, str], *, phonetic: bool = False) -> str:
    code = str(item.get("code", ""))
    name = str(item.get("name", ""))
    student_url = str(item.get("student_url", ""))
    api_url = str(item.get("api_url", ""))
    aliases = INSTITUTION_ALIASES.get(code, "")
    domains = f"{_domain_keywords(student_url)} {_domain_keywords(api_url)}"
    norm_name = normalize_search_text(name, phonetic=phonetic)
    core_name = _strip_honorific(norm_name)
    # Bosh harflardan avtomatik qisqartma (abbreviatura) yasash
    acronym = "".join(w[0] for w in core_name.split() if w and w not in ("va", "nomidagi"))
    full_acronym = "".join(w[0] for w in norm_name.split() if w and w not in ("va", "nomidagi"))
    combined = f"{name} {aliases} {domains} {acronym} {full_acronym} {code}"
    return normalize_search_text(combined, phonetic=phonetic)


def match_hemis_institutions(
    catalog: list[dict[str, str]], query: str
) -> list[dict[str, str]]:
    """Foydalanuvchi so'rovi bo'yicha OTMlarni aqlli qidirish va saralash."""
    norm_query = normalize_search_text(query, phonetic=False)
    if not norm_query:
        return []

    # 1 harfli qidiruv: OTM nomi yoki "X nomidagi" dan keyingi asosiy nomi shu harfdan boshlansa
    if len(norm_query) == 1:
        single_matches = []
        for item in catalog:
            norm_name = normalize_search_text(item.get("name", ""))
            core_name = _strip_honorific(norm_name)
            if norm_name.startswith(norm_query) or core_name.startswith(norm_query):
                single_matches.append(item)
        single_matches.sort(
            key=lambda it: (
                not normalize_search_text(it.get("name", "")).startswith(norm_query),
                normalize_search_text(it.get("name", "")),
            )
        )
        return single_matches

    query_tokens = norm_query.split()
    scored: list[tuple[tuple[int, int, str], dict[str, str]]] = []

    for item in catalog:
        norm_name = normalize_search_text(item.get("name", ""))
        core_name = _strip_honorific(norm_name)
        blob = _institution_search_blob(item, phonetic=False)

        if all(tok in blob for tok in query_tokens):
            # Saralash ustuvorligi (kichik raqam = yuqoriroq o'rin)
            if norm_name.startswith(norm_query) or core_name.startswith(norm_query):
                rank = 0
            elif norm_query in norm_name:
                rank = 1
            elif all(tok in norm_name for tok in query_tokens):
                rank = 2
            else:
                rank = 3
            scored.append(((rank, len(norm_name), norm_name), item))

    # Agar oddiy qidiruvda topilmasa, fonetik (x/h, q/k) qidiruvni sinab ko'ramiz
    if not scored:
        phon_query = normalize_search_text(query, phonetic=True)
        phon_tokens = phon_query.split()
        for item in catalog:
            norm_name = normalize_search_text(item.get("name", ""), phonetic=True)
            blob = _institution_search_blob(item, phonetic=True)
            if all(tok in blob for tok in phon_tokens):
                rank = 1 if phon_query in norm_name else 2
                scored.append(((rank, len(norm_name), norm_name), item))

    scored.sort(key=lambda pair: pair[0])
    return [item for _, item in scored]


async def get_hemis_institutions() -> list[dict[str, str]]:
    global _cached_at, _cached_institutions

    if _cached_institutions and time.monotonic() - _cached_at < CACHE_SECONDS:
        return _cached_institutions

    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(timeout=timeout) as client:
        async with client.get(CATALOG_URL) as response:
            response.raise_for_status()
            payload = await response.json(content_type=None)

    records = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(records, list):
        raise ValueError("HEMIS institution directory has an invalid response.")

    institutions = []
    seen_codes = set()

    # Qo'shimcha / qayta nomlangan OTMlarni (masalan, SamDAQU) ro'yxatga qo'shish
    for extra in SUPPLEMENTAL_INSTITUTIONS:
        seen_codes.add(extra["code"])
        institutions.append(dict(extra))

    for record in records:
        if not isinstance(record, dict):
            continue
        code = str(record.get("code", "")).strip()
        name = str(record.get("name", "")).strip()
        api_url = str(record.get("api_url", "")).strip()
        student_url = str(record.get("student_url", "")).strip()
        if (
            not code
            or code in seen_codes
            or not name
            or not _is_https_url(api_url)
            or not _is_https_url(student_url)
        ):
            continue
        seen_codes.add(code)
        institutions.append({
            "code": code,
            "name": name,
            "api_url": api_url,
            "student_url": student_url,
        })

    if len(institutions) <= len(SUPPLEMENTAL_INSTITUTIONS):
        raise ValueError("HEMIS institution directory contains no usable institutions.")

    _cached_institutions = institutions
    _cached_at = time.monotonic()
    return _cached_institutions
