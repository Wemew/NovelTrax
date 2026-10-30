import re
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

def get_paged_url(base_url, page_num):
    parsed = urlparse(base_url)
    path = parsed.path.rstrip('/')
    path = re.sub(r'/page/\d+', '', path)
    new_path = f"{path}/page/{page_num}/" if path else f"/page/{page_num}/"
    pretty_url = urlunparse((parsed.scheme, parsed.netloc, new_path, '', parsed.query, ''))

    query = parse_qs(parsed.query)
    query['paged'] = [str(page_num)]
    new_query = urlencode(query, doseq=True)
    param_url = urlunparse((parsed.scheme, parsed.netloc, path or '/', '', new_query, ''))
    return pretty_url, param_url

for u in ["https://jnovels.com/?s=epub", "https://jnovels.com/page/1/?s=epub", "https://jnovels.com/category/light-novel/"]:
    pretty, param = get_paged_url(u, 2)
    print(f"Base:   {u}")
    print(f"  Page 2 (Pretty): {pretty}")
    print(f"  Page 2 (Param):  {param}")
