import os
import re
import json
import logging
import argparse
from typing import List, Dict, Optional
from urllib.parse import urlsplit, urlunsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from api_calls.call_rh import RunningHubResponseError, site_base_url, validate_response

logger = logging.getLogger(__name__)


def create_session(retries: int = 3, backoff: float = 0.3, status_forcelist=(500, 502, 504)) -> requests.Session:
    """Create a requests.Session with retry/backoff behavior."""
    session = requests.Session()
    retry = Retry(total=retries, backoff_factor=backoff, status_forcelist=status_forcelist, allowed_methods=frozenset(["GET", "POST"]))
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def get_runninghub_apps(user_id: Optional[str] = None,
                        page: int = 1,
                        page_size: int = 20,
                        n: Optional[int] = None,
                        session: Optional[requests.Session] = None,
                        timeout: int = 10,
                        base_url: str = "https://www.runninghub.ai",
                        fetch_details: bool = False) -> List[Dict]:
    """
    Query RunningHub for the user's webapps and return a list of apps.

    Returns a list of dicts with keys: `webappId`, `webappName`, `url` and any other fields returned by the API.
    """
    user_id = user_id or os.environ.get("RUNNINGHUB_USER_ID")
    if not user_id:
        raise ValueError("user_id must be provided either as argument or via RUNNINGHUB_USER_ID env var")
    if not isinstance(page, int) or isinstance(page, bool) or page < 1:
        raise ValueError("page must be a positive integer")
    if not isinstance(page_size, int) or isinstance(page_size, bool) or page_size < 1:
        raise ValueError("page_size must be a positive integer")
    if n is not None and (not isinstance(n, int) or isinstance(n, bool) or n < 0):
        raise ValueError("n must be a non-negative integer or None")
    if n == 0:
        return []
    base_url = site_base_url(base_url)

    owns_session = session is None
    if owns_session:
        session = create_session()
    try:
        url = f"{base_url}/api/webapp/user/list"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Content-Type": "application/json",
            "Origin": base_url,
            "Referer": f"{base_url}/user-center/{user_id}/webapp"
        }

        # The RunningHub API expects paging keys named `current` and `size` (site JSON shows
        # {"current":..., "size":...}). Older callers may think `page`/`pageSize`, but
        # use `current`/`size` to get the expected page_size results.
        # If `n` is provided, treat it as the total number of items to fetch (first n apps).
        # Otherwise fetch a single page (backwards compatible).
        result: List[Dict] = []
        seen_ids = set()

        current_page = page
        while True:
            payload = {
                "userId": user_id,
                "current": current_page,
                "size": page_size,
                "keyword": "",
                "sortType": "newest"
            }

            logger.debug("Requesting RunningHub app list: %s (page=%s,size=%s)", url, current_page, page_size)
            resp = session.post(url, headers=headers, json=payload, timeout=timeout)
            resp.raise_for_status()

            try:
                res_json = validate_response(resp.json(), 'List applications', require_code=False)
            except ValueError:
                logger.error("Non-JSON response from RunningHub for app list (page=%s)", current_page)
                raise RunningHubResponseError('List applications returned invalid JSON') from None

            data = res_json.get('data')
            if not isinstance(data, dict):
                raise RunningHubResponseError('List applications returned an invalid response: data must be an object')
            records = data.get('records', [])
            if not isinstance(records, list) or not all(isinstance(item, dict) for item in records):
                raise RunningHubResponseError('List applications returned an invalid response: records must be a list of objects')
            if not records:
                break

            count_before_page = len(result)
            for item in records:
                app_id = item.get("webappId") or item.get("id")
                if not app_id:
                    continue
                if app_id in seen_ids:
                    continue
                seen_ids.add(app_id)

                name = item.get("webappName") or item.get("name") or "Unnamed"
                full_url = f"{base_url}/webapp/{app_id}"
                out = {**item}
                out.update({"webappId": app_id, "webappName": name, "url": full_url})

                if fetch_details:
                    try:
                        detail = scrape_runninghub_detail(full_url, session=session, timeout=timeout, api_base=base_url)
                        out["description"] = detail.get("description") or detail.get("intro") or ""
                        covers = detail.get("covers") or []
                        thumbnail = ""
                        if isinstance(covers, list) and covers:
                            thumbnail = covers[0].get("thumbnailUri") or covers[0].get("url") or ""
                        out["thumbnail_uri"] = thumbnail
                    except Exception:
                        out.setdefault("description", "")
                        out.setdefault("thumbnail_uri", "")

                result.append(out)
                if n is not None and len(result) >= n:
                    break

            # Some servers repeat the last page instead of returning an empty one.
            # With no new IDs, another request cannot make reliable progress.
            if len(result) == count_before_page:
                logger.warning("Stopping RunningHub pagination: page %s contained no new app IDs", current_page)
                break

            # If caller requested a specific number of items, stop when reached.
            if n is not None and len(result) >= n:
                break

            # If no `n` specified (back-compat), fetch only the initial page.
            if n is None:
                break

            # Otherwise advance to next page and continue collecting.
            current_page += 1

        if n is not None:
            return result[:n]
        return result
    finally:
        if owns_session:
            session.close()


def scrape_runninghub_detail(page_url: str,
                              session: Optional[requests.Session] = None,
                              timeout: int = 10,
                              api_base: Optional[str] = None) -> Dict:
    """
    Fetch detail for a single RunningHub webapp given a page URL.

    Returns a dict with the API response `data` payload. Unless api_base is
    supplied explicitly, use the same website as the application page.
    """
    page_url = (page_url or '').strip()
    if '://' not in page_url:
        page_url = 'https://' + page_url
    try:
        page = urlsplit(page_url)
        page_origin = site_base_url(urlunsplit((page.scheme, page.netloc, '', '', '')))
    except ValueError:
        raise ValueError('Application page URL must contain a valid HTTP(S) website') from None
    api_base = site_base_url(api_base) if api_base is not None else page_origin
    # Query and fragment fields are not needed for the detail request.
    page_url = page_origin + page.path
    # Try multiple patterns to extract ID
    patterns = [r"ai-detail/(\d+)", r"/webapp/(\d+)", r"/(\d{15,})"]
    current_id = None
    for p in patterns:
        m = re.search(p, page.path)
        if m:
            current_id = m.group(1)
            break

    if not current_id:
        raise ValueError("Could not extract webapp ID from application page URL")

    owns_session = session is None
    if owns_session:
        session = create_session()
    try:
        api_url = f"{api_base}/api/webapp/detail"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Content-Type": "application/json",
            "Origin": api_base,
            "Referer": page_url
        }

        payload = {"webappId": current_id}
        logger.debug("Requesting detail for id %s at %s", current_id, api_url)
        resp = session.post(api_url, headers=headers, json=payload, timeout=timeout)
        resp.raise_for_status()
        try:
            res_json = validate_response(resp.json(), 'Get application details', require_code=False)
        except ValueError:
            logger.error("Non-JSON response from RunningHub detail API")
            raise RunningHubResponseError('Get application details returned invalid JSON') from None

        data = res_json.get("data")
        if not isinstance(data, dict):
            raise RunningHubResponseError('Get application details returned an invalid response: data must be an object')

        return data
    finally:
        if owns_session:
            session.close()


def _print_apps(apps: List[Dict]):
    for a in apps:
        print(f"应用名称: {a.get('webappName')}")
        print(f"应用网址: {a.get('url')}")
        print("-" * 30)


def main():
    parser = argparse.ArgumentParser(description="RunningHub helper: list apps or fetch detail")
    parser.add_argument("--list", action="store_true", help="列出用户的 RunningHub 应用")
    parser.add_argument("--detail", type=str, help="抓取指定页面的详情（传入页面 URL）")
    parser.add_argument("--user-id", type=str, help="RunningHub userId (可用环境变量 RUNNINGHUB_USER_ID)")
    parser.add_argument("-n", "--count", type=int, default=None, help="获取前 n 个应用（数量），不指定则只获取一页")
    parser.add_argument("--debug", action="store_true", help="开启调试日志")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    with create_session() as session:

        if args.list:
            user_id = args.user_id or os.environ.get("RUNNINGHUB_USER_ID") or "1911823721911500801"
            apps = get_runninghub_apps(user_id=user_id, session=session, n=args.count)
            _print_apps(apps)
            return

        if args.detail:
            data = scrape_runninghub_detail(args.detail, session=session)
            print(json.dumps(data, ensure_ascii=False, indent=2))
            return

        parser.print_help()


if __name__ == "__main__":
    main()
