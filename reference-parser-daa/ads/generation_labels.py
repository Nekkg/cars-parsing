"""Source-independent parsing of explicit generation facts."""
import re
import unicodedata


def ordinal(label):
    text = unicodedata.normalize('NFKC', str(label or '')).strip()
    match = re.search(r'\b(\d{1,2})(?:\s*[-–]?\s*[еийя])?\s*(?:поколени\w*|generation|gen)\b', text, re.I)
    if not match:
        match = re.search(r'\b(?:поколени\w*|generation)\s*(\d{1,2})\b', text, re.I)
    if match:
        return int(match[1]) if 0 < int(match[1]) <= 30 else None
    match = re.match(r'^([IVX]+)(?=\W|$)', text)
    if not match or not re.fullmatch(r'X{0,3}(?:IX|IV|V?I{0,3})', match[1]):
        return None
    values = {'I':1,'V':5,'X':10}
    nums = [values[c] for c in match[1]]
    return sum(-n if i+1 < len(nums) and n < nums[i+1] else n for i,n in enumerate(nums))


def facelift(label):
    text = str(label or '').casefold()
    if re.search(r'\b(?:дорест\w*|pre[\s-]?facelift|без\s+рестайл\w*)',text):
        return 0
    token = r'(?:рестайл\w*|restyl\w*|facelift\w*)'
    match = re.search(r'(?<!\w)(?:(\d+)\s*(?:-?\s*[йе])?\s*)?' + token + r'(?:\s*(\d+))?', text)
    return int(match[1] or match[2] or 1) if match else 0


def production_range(label):
    match = re.search(r'\b((?:19|20)\d{2})\s*[-–—−]\s*((?:19|20)\d{2}|н\.?\s*в\.?|present)', str(label or ''), re.I)
    if not match:
        return None
    start = int(match[1])
    end = int(match[2]) if match[2].isdigit() else None
    return (start,end) if end is None or end >= start else None
