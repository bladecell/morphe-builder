import json
import logging
import os
import re
import shutil
import tempfile
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode, urljoin, urlparse, urlunparse

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Playwright, sync_playwright
from selectolax.parser import HTMLParser, Node
from shardx import BrowserSession, ShardX  # type: ignore[import-untyped]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("ApkMirrorDownloader")


class _StdoutHandler(logging.Handler):
    """Prints to the current sys.stdout, so scraper logs show up in the
    build console (which swaps sys.stdout during a build)."""

    def emit(self, record: logging.LogRecord) -> None:
        """Print the formatted record to the current sys.stdout."""
        print(self.format(record))


_stdout_handler = _StdoutHandler()
_stdout_handler.setFormatter(
    logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
)
logger.addHandler(_stdout_handler)
logger.propagate = False
SHARDX_ARGS = ("--disable-dev-shm-usage",)


class Arch(str, Enum):
    UNIVERSAL = "universal"
    ARM64_V8A = "arm64-v8a"
    ARMEABI = "armeabi"
    ARMEABI_V7A = "armeabi-v7a"
    MIPS = "mips"
    MIPS64 = "mips64"
    X86 = "x86"
    X86_64 = "x86_64"


class Dpi(str, Enum):
    DPI_120 = "120"
    DPI_160 = "160"
    DPI_213 = "213"
    DPI_240 = "240"
    DPI_280 = "280"
    DPI_320 = "320"
    DPI_360 = "360"
    DPI_400 = "400"
    DPI_420 = "420"
    DPI_480 = "480"
    DPI_560 = "560"
    DPI_640 = "640"
    NODPI = "nodpi"


class BundleType(str, Enum):
    APKM_BUNDLES = "apkm_bundles"
    APK_FILES = "apk_files"


class AndroidVariantMetadataError(RuntimeError):
    """A constrained APKMirror variant lacks an unambiguous Android floor."""


class AndroidVariantInspectionError(RuntimeError):
    """APKEditor could not execute to inspect a constrained artifact."""


class AndroidVariantUnavailableError(RuntimeError):
    """No APKMirror variant satisfies the requested Android API floor."""


_ANDROID_MINIMUM_LABEL = re.compile(
    r"\bAndroid\s+(\d+(?:\.\d+)?[LW]?)\s*(?:\+|and\s+up)",
    re.IGNORECASE,
)
_ANDROID_RELEASE_APIS = {
    "1.0": 1,
    "1.1": 2,
    "1.5": 3,
    "1.6": 4,
    "2.0": 5,
    "2.0.1": 6,
    "2.1": 7,
    "2.2": 8,
    "2.3": 9,
    "2.3.3": 10,
    "3.0": 11,
    "3.1": 12,
    "3.2": 13,
    "4.0": 14,
    "4.0.3": 15,
    "4.1": 16,
    "4.2": 17,
    "4.3": 18,
    "4.4": 19,
    "4.4w": 20,
    "5.0": 21,
    "5.1": 22,
    "6": 23,
    "6.0": 23,
    "7": 24,
    "7.0": 24,
    "7.1": 25,
    "8": 26,
    "8.0": 26,
    "8.1": 27,
    "9": 28,
    "10": 29,
    "11": 30,
    "12": 31,
    "12l": 32,
    "13": 33,
    "14": 34,
    "15": 35,
    "16": 36,
}


def _android_minimum_api(row_text: str) -> int:
    """Convert one explicit Android lower-bound label to its API level."""
    labels = {
        match.group(1).lower() for match in _ANDROID_MINIMUM_LABEL.finditer(row_text)
    }
    if len(labels) != 1:
        raise AndroidVariantMetadataError(
            "Could not verify the Android version requirement for an APK variant"
        )
    label = next(iter(labels))
    api_level = _ANDROID_RELEASE_APIS.get(label)
    if api_level is None and label.endswith(".0"):
        api_level = _ANDROID_RELEASE_APIS.get(label[:-2])
    if api_level is None:
        raise AndroidVariantMetadataError(
            "Could not verify the Android version requirement for an APK variant"
        )
    return api_level


_PAGE_MINIMUM_API = re.compile(r"Min:\s*Android[^()]*\(API\s*(\d+)\)", re.IGNORECASE)


def _page_minimum_api(parser: HTMLParser) -> int | None:
    """Read the "Min: Android 11 (API 30)" spec of a variant page, if present."""
    for node in parser.css(".appspec-value"):
        if match := _PAGE_MINIMUM_API.search(node.text(strip=True)):
            return int(match.group(1))
    return None


@dataclass
class AppMetadata:
    title: str
    package_name: str
    version: str
    file_size: str
    icon_url: str
    download_url: str

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=4)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Stop urllib from following redirects so the Location can be inspected."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        """Decline every redirect."""
        return None  # noqa: RET501 - urllib expects None to decline


MORPHE_RESOLVER_URL = "https://api.morphe.software"


def resolve_variant_url(
    package_name: str, version: str, arch: str, timeout: float = 20
) -> str | None:
    """Ask the Morphe API which APKMirror variant page matches an exact version.

    The API answers with a redirect to the variant page, or to its own fallback
    route, which is followed for at most two hops.

    Args:
        package_name: Android package id.
        version: Exact app version.
        arch: ABI such as "arm64-v8a" or "universal".
        timeout: Seconds to wait per request.

    Returns:
        An https www.apkmirror.com URL, or None when the API has no match, is
        unreachable, or redirects anywhere else.
    """
    opener = urllib.request.build_opener(_NoRedirect)
    url = (
        f"{MORPHE_RESOLVER_URL}/v2/web-search/"
        f"{quote(package_name, safe='')}~{quote(version, safe='')}~{quote(arch, safe='')}"
    )
    for _ in range(3):
        try:
            opener.open(
                urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}),
                timeout=timeout,
            )
        except urllib.error.HTTPError as error:
            location = error.headers.get("Location")
            if error.code not in (301, 302, 303, 307, 308) or not location:
                logger.warning(f"Morphe resolver returned HTTP {error.code}")
                return None
            url = urljoin(url, location)
        except (urllib.error.URLError, TimeoutError) as error:
            logger.warning(f"Morphe resolver unreachable: {error}")
            return None
        else:
            return None
        parsed = urlparse(url)
        if parsed.scheme == "https" and parsed.netloc == "www.apkmirror.com":
            return url
        if parsed.netloc != urlparse(MORPHE_RESOLVER_URL).netloc:
            logger.warning("Morphe resolver redirected to an unexpected host")
            return None
    return None


class ApkMirror_Downloader:
    BASE_URL = "https://www.apkmirror.com"
    BASE_NETLOC = "www.apkmirror.com"

    def __init__(self) -> None:
        """Launch the browser session used for all navigation."""
        self._launch()

    def _launch(self) -> None:
        """Start a fresh ShardX engine and attach Playwright to it over CDP.

        The engine is headless: ShardX passes APKMirror's Cloudflare challenge
        that way, and headed under Xvfb it does not. Each launch gets a new
        throwaway profile.

        Raises:
            RuntimeError: The engine did not expose its debugging endpoint.
        """
        logger.info("Initializing ShardX browser session...")
        self._profiles_dir = Path(tempfile.mkdtemp(prefix="shardx-profiles-"))
        self._engine: BrowserSession | None = None
        self._playwright: Playwright | None = None
        try:
            sdk = ShardX(profiles_dir=str(self._profiles_dir))
            # The engine is fetched at image build time; do not let a changed
            # upstream manifest trigger a download while the app runs.
            sdk.runtime._checked_in_process = sdk.runtime.installed
            profile = sdk.create_profile(platform="Windows")
            # Chromium refuses its sandbox as root, which is how the container runs.
            extra_args = [
                *SHARDX_ARGS,
                *(["--no-sandbox"] if os.geteuid() == 0 else []),
            ]
            self._engine = sdk.launch(
                profile,
                cdp=True,
                headless=True,
                extra_args=extra_args,
            )
            if not self._engine.cdp_url:
                raise RuntimeError("ShardX engine did not expose a CDP endpoint")
            self._playwright = sync_playwright().start()
            self.browser = self._playwright.chromium.connect_over_cdp(
                self._engine.cdp_url
            )
            self.page = self.browser.contexts[0].new_page()
        except Exception:
            self._teardown()
            raise

    def _teardown(self) -> None:
        """Stop Playwright, the engine process and the profile; log failures."""
        steps = (
            ("browser", lambda: self.browser.close()),
            ("playwright", lambda: self._playwright.stop()),
            ("engine", lambda: self._engine.stop()),
        )
        for name, step in steps:
            try:
                step()
            except (AttributeError, PlaywrightError, OSError) as error:
                logger.debug(f"Closing the {name} failed: {error}")
        self.browser = None
        shutil.rmtree(self._profiles_dir, ignore_errors=True)

    def _goto(self, url: str) -> None:
        """Navigate to url, relaunching the browser once if it has died."""
        try:
            self.page.goto(url, wait_until="domcontentloaded")
        except PlaywrightError as e:
            # Timeouts are PlaywrightError too; only a dead browser is retried
            message = str(e).lower()
            engine_dead = self._engine is not None and self._engine.process.poll()
            if not engine_dead and not any(
                word in message for word in ("crash", "closed", "disconnected")
            ):
                raise
            logger.warning(
                f"Browser died during navigation ({e}). "
                "Relaunching and retrying once..."
            )
            self._teardown()
            self._launch()
            self.page.goto(url, wait_until="domcontentloaded")

    def _build_search_url(
        self, package_name, version=None, archs=None, dpis=None, bundles=None
    ):
        if archs is None:
            archs = [item.value for item in Arch]
        if dpis is None:
            dpis = [item.value for item in Dpi]
        if bundles is None:
            bundles = [item.value for item in BundleType]

        search_query = package_name
        if version:
            search_query += f" {version}"

        query_params = [
            ("post_type", "app_release"),
            ("searchtype", "apk"),
            ("s", search_query),
        ]
        for arch in archs:
            val = arch.value if isinstance(arch, Enum) else arch
            query_params.append(("arch[]", val))
        for dpi in dpis:
            val = dpi.value if isinstance(dpi, Enum) else dpi
            query_params.append(("dpi[]", val))
        for bundle in bundles:
            val = bundle.value if isinstance(bundle, Enum) else bundle
            query_params.append(("bundles[]", val))

        encoded_query = urlencode(query_params, doseq=False)
        return urlunparse(("https", self.BASE_NETLOC, "/", "", encoded_query, ""))

    def search(
        self,
        package_name,
        version=None,
        only_release=True,
        preferred_arch=Arch.ARM64_V8A,
        preferred_dpi=Dpi.NODPI,
        prefer_bundle=False,
        minimum_sdk: int | None = None,
    ) -> AppMetadata | None:
        if version:
            resolved = self._search_via_morphe(
                package_name, version, preferred_arch, minimum_sdk
            )
            if resolved:
                return resolved
        url = self._build_search_url(package_name, version)
        logger.info(f"Navigating to search results endpoint for '{package_name}'...")
        try:
            self._goto(url)
        except Exception as e:
            logger.error(f"Navigation to search page failed: {e}")
            return None

        try:
            self.page.wait_for_selector(
                ".appRow, .variants-table, .downloadButton", timeout=15000
            )
        except Exception:  # noqa: BLE001 - any failure means the page did not load
            logger.error("Timeout reached. No valid target layouts loaded.")
            return None

        current_html = self.page.content()
        parser = HTMLParser(current_html)

        # 1. We landed on a standard Search Results Page list
        if parser.css_first(".appRow"):
            logger.info(
                "Multiple results found. Extracting matching release row target..."
            )
            master_row = self._extract_first_valid_row(parser, version, only_release)
            if not master_row:
                logger.warning(
                    "No matching release rows survived filtering parameter constraints."
                )
                return None

            master_release_url = urljoin(
                self.BASE_URL,
                master_row.css("a.fontBlack")[0].attributes.get("href", ""),
            )
            logger.info(f"Navigating to release page: {master_release_url}")
            self._goto(master_release_url)

            # Wait for whatever layout loads next (could be a variant table OR direct final download link)
            try:
                self.page.wait_for_selector(
                    ".variants-table, .downloadButton", timeout=15000
                )
            except Exception:
                logger.error("Failed to load subsequent release page components.")
                return None

            parser = HTMLParser(self.page.content())

        # 2. Check if we have a table of variants or if we skipped directly to the final layout page
        if parser.css_first(".variants-table"):
            logger.info(
                "Variant options table detected. Processing architectural score matching..."
            )
            variant_page_url = self._get_best_variant(
                parser,
                preferred_arch,
                preferred_dpi,
                prefer_bundle,
                minimum_sdk=minimum_sdk,
            )
            if not variant_page_url:
                if minimum_sdk is not None:
                    raise AndroidVariantUnavailableError(
                        "No APKMirror variant meets the required Android version"
                    )
                logger.warning(
                    "Failed to calculate an eligible architectural match variant row target link."
                )
                return None

            logger.info(
                f"Navigating to isolated variant details page: {variant_page_url}"
            )
            self._goto(variant_page_url)
            try:
                self.page.wait_for_selector(".downloadButton", timeout=15000)
            except Exception:
                logger.error("Final download button missing from layout DOM context.")
                return None

            parser = HTMLParser(self.page.content())
        else:
            if minimum_sdk is not None:
                raise AndroidVariantMetadataError(
                    "APKMirror did not provide Android version metadata for variants"
                )
            logger.info(
                "Single variant detected! Skipped variants grid directly to final download layout."
            )

        logger.info(
            "Successfully extracted complete validated package payload asset target info."
        )
        return self._extract_final_metadata(parser)

    def _search_via_morphe(
        self,
        package_name: str,
        version: str,
        preferred_arch: Arch | str,
        minimum_sdk: int | None = None,
    ) -> AppMetadata | None:
        """Open the variant page the Morphe API resolves and read its metadata.

        Args:
            package_name: Android package id.
            version: Exact app version.
            preferred_arch: ABI to ask the API for.
            minimum_sdk: Required minimum Android API. The page must state a
                "Min: Android N (API X)" with X at or above it.

        Returns:
            The metadata, or None when the API or the page did not deliver or
            does not meet minimum_sdk, so the caller falls back to searching
            APKMirror itself.
        """
        arch = (
            preferred_arch.value if isinstance(preferred_arch, Enum) else preferred_arch
        )
        variant_url = resolve_variant_url(package_name, version, arch)
        if not variant_url:
            return None
        logger.info(f"Morphe API resolved {package_name} {version}: {variant_url}")
        try:
            self._goto(variant_url)
            self.page.wait_for_selector(".downloadButton", timeout=15000)
        except Exception:  # noqa: BLE001 - any failure means the page did not load
            logger.warning("Resolved variant page did not load; searching instead.")
            return None
        parser = HTMLParser(self.page.content())
        if minimum_sdk is not None:
            page_minimum = _page_minimum_api(parser)
            if page_minimum is None or page_minimum < minimum_sdk:
                logger.info(
                    "Resolved variant does not verifiably meet Android API "
                    f"{minimum_sdk}; searching instead."
                )
                return None
        return self._extract_final_metadata(parser)

    def latest_version(
        self, package_name: str, only_release: bool = True
    ) -> str | None:
        """Return the newest version listed on the search results page.

        Reads the search results only; the release and variant pages are
        not visited.

        Args:
            package_name: Android package id to search for, e.g.
                "com.instagram.android".
            only_release: Skip alpha, beta, dev and canary rows.

        Returns:
            The version string of the first matching result row, or None
            if no row matches or the row has no version details.

        Raises:
            playwright.sync_api.Error: If navigation fails, or no result
                row shows up within 15 seconds (a TimeoutError).

        Example:
            >>> downloader.latest_version("com.instagram.android")
            '449.0.0.52.84'
        """
        logger.info(f"Looking up newest version for '{package_name}'...")
        self._goto(self._build_search_url(package_name))
        self.page.wait_for_selector(".appRow", timeout=15000)

        row = self._extract_first_valid_row(
            HTMLParser(self.page.content()), None, only_release
        )
        if not row:
            logger.warning("No matching release rows found in search results.")
            return None

        # Each result row is followed by an .infoSlide holding its details
        info = row.next
        while info is not None and info.tag.startswith("-"):
            info = info.next
        if info is None:
            return None
        for name in info.css(".infoSlide-name"):
            if name.text(strip=True) != "Version:" or name.parent is None:
                continue
            value = name.parent.css_first(".infoSlide-value")
            return value.text(strip=True) if value else None
        return None

    def _extract_first_valid_row(
        self, parser: HTMLParser, target_version: str | None, only_release: bool
    ) -> Node | None:
        """Return the first result row that passes the version filters."""
        app_rows = parser.css(".appRow")
        for row in app_rows:
            title_element = row.css_first("a.fontBlack")
            if not title_element:
                continue

            title_text = title_element.text(strip=True).lower()

            if (
                not target_version
                and only_release
                and any(x in title_text for x in ["alpha", "beta", "dev", "canary"])
            ):
                continue

            if target_version and target_version.lower() not in title_text:
                continue

            return row

        return None

    def _get_best_variant(
        self,
        parser,
        preferred_arch,
        preferred_dpi,
        prefer_bundle,
        minimum_sdk: int | None = None,
    ) -> str | None:
        variant_rows = parser.css(".variants-table .table-row")
        if not variant_rows:
            return None

        scored_variants = []
        for row in variant_rows:
            if row.css_first(".variant-min-width"):
                continue

            cells = row.css(".table-cell")
            if len(cells) < 4:
                continue

            if minimum_sdk is not None:
                try:
                    row_minimum_sdk = _android_minimum_api(cells[2].text(strip=True))
                except AndroidVariantMetadataError:
                    continue
                if row_minimum_sdk < minimum_sdk:
                    continue

            variant_cell = cells[0]
            arch_text = cells[1].text(strip=True).lower()
            dpi_text = cells[3].text(strip=True).lower()

            badge = variant_cell.css_first(".apkm-badge")
            is_bundle = badge and "BUNDLE" in badge.text().upper()

            link_element = variant_cell.css_first("a.accent_color")
            if not link_element:
                continue

            relative_url = link_element.attributes.get("href", "")
            absolute_url = urljoin(self.BASE_URL, relative_url)

            score = 0
            if preferred_arch.lower() in arch_text:
                score += 10
            elif "universal" in arch_text:
                score += 5

            if preferred_dpi.lower() in dpi_text:
                score += 5
            elif "nodpi" in dpi_text:
                score += 3

            if is_bundle == prefer_bundle:
                score += 2

            scored_variants.append({"url": absolute_url, "score": score})

        if scored_variants:
            scored_variants.sort(key=lambda x: x["score"], reverse=True)
            return scored_variants[0]["url"]
        return None

    def _extract_final_metadata(self, parser) -> AppMetadata:
        package_name, version_string, file_size = "Unknown", "Unknown", "Unknown"

        spec_values = parser.css(".appspec-value")
        for val in spec_values:
            text = val.text(strip=True)
            if "Package:" in text:
                package_name = text.split("Package:")[-1].split("Downloads:")[0].strip()
            if "Version:" in text:
                raw_version = text.split("Version:")[-1].strip()
                version_string = (
                    raw_version.split("(")[0].split("Languages:")[0].strip()
                )
            if "MB" in text or "bytes" in text:
                file_size = text.split("(")[0].strip()

        icon_url = "Unknown"
        if primary_image := parser.css_first("img#primaryimage"):
            icon_url = urljoin(self.BASE_URL, primary_image.attributes.get("src", ""))

        download_url = "Unknown"
        if download_button := parser.css_first("a.downloadButton"):
            download_url = urljoin(
                self.BASE_URL, download_button.attributes.get("href", "")
            )

        title_element = parser.css_first("h2.tabs-header")
        title = title_element.text(strip=True) if title_element else "Unknown"

        return AppMetadata(
            title=title,
            package_name=package_name,
            version=version_string,
            file_size=file_size,
            icon_url=icon_url,
            download_url=download_url,
        )

    def download_file(self, target_url, dest_directory, filename) -> str | None:
        if not target_url or target_url == "Unknown":
            logger.error(
                f"Cannot download asset configuration target link source pointer reference is value '{target_url}'"
            )
            return None

        os.makedirs(dest_directory, exist_ok=True)
        dest_path = os.path.join(dest_directory, filename)

        if "/download/" in target_url:
            return self._download_binary_via_browser(target_url, dest_path)

        logger.info(f"Downloading static file asset via socket stream: {filename}")
        req = urllib.request.Request(target_url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            with (
                urllib.request.urlopen(req) as response,
                open(dest_path, "wb") as out_file,
            ):
                while chunk := response.read(1024 * 64):
                    out_file.write(chunk)
            logger.info(f"File payload asset safely saved locally: {filename}")
            return dest_path
        except Exception as e:  # noqa: BLE001 - fall back to the browser
            logger.warning(
                f"Standard chunk connection dropped: {e}. Falling back to browser..."
            )
            return self._download_binary_via_browser(target_url, dest_path)

    def _download_binary_via_browser(self, target_url, dest_path) -> str | None:
        logger.info(
            "Navigating to intermediate redirect page to capture explicit token..."
        )
        try:
            self._goto(target_url)

            try:
                self.page.wait_for_selector("#download-link", timeout=10000)
            except Exception:
                logger.error(
                    "Timed out waiting for direct '#download-link' to appear on the page."
                )
                return None

            parser = HTMLParser(self.page.content())
            download_anchor = parser.css_first("a#download-link")

            if not download_anchor:
                logger.error(
                    "Could not find the 'a#download-link' element in the page source."
                )
                return None

            relative_file_url = download_anchor.attributes.get("href", "")
            if not relative_file_url:
                logger.error(
                    "The '#download-link' element is missing its href attribute."
                )
                return None

            absolute_file_url = urljoin(self.BASE_URL, relative_file_url)
            logger.info(f"Extracted direct tokenized file link: {absolute_file_url}")
            logger.info(
                f"Triggering direct browser download pipeline for: {os.path.basename(dest_path)}"
            )

            with self.page.expect_download(timeout=60000) as download_info:
                try:
                    self.page.goto(absolute_file_url, wait_until="domcontentloaded")
                except Exception as e:
                    if "Download is starting" in str(e):
                        logger.debug(
                            "Absorbed expected browser navigation switch to download stream context."
                        )
                    else:
                        raise e

            download = download_info.value
            download.save_as(dest_path)

            logger.info(
                "File payload saved successfully via direct token exploitation."
            )
            return dest_path

        except Exception as e:  # noqa: BLE001 - fall back to the browser
            logger.error(f"Direct browser download extraction sequence failed: {e}")
            return None

    def close(self) -> None:
        """Close the page connection, stop the engine and delete its profile."""
        logger.info("Closing the ShardX browser and its engine process.")
        self._teardown()


if __name__ == "__main__":
    downloader = ApkMirror_Downloader()
    output_dir = "./test_downloads"

    try:
        # This will now adapt dynamically and pass perfectly!
        app_data = downloader.search(
            package_name="fr.tramb.park4night",
            version="7.1.11",
            only_release=True,
        )

        if app_data:
            logger.info("Metadata collected successfully:")
            print(app_data.to_json())

            downloader.download_file(
                target_url=app_data.icon_url,
                dest_directory=output_dir,
                filename="icon.png",
            )

            downloader.download_file(
                target_url=app_data.download_url,
                dest_directory=output_dir,
                filename=f"{app_data.package_name}_{app_data.version}.apk",
            )

    finally:
        downloader.close()
