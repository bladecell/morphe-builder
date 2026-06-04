import os
import json
import logging
import urllib.request
from dataclasses import dataclass, asdict
from enum import Enum
from urllib.parse import urlencode, urlunparse, urljoin
from cloakbrowser import launch
from selectolax.parser import HTMLParser

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("ApkMirrorDownloader")


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


class ApkMirror_Downloader:
    BASE_URL = "https://www.apkmirror.com"
    BASE_NETLOC = "www.apkmirror.com"

    def __init__(self):
        logger.info("Initializing cloakbrowser context session...")
        self.browser = launch()
        self.page = self.browser.new_page()

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
    ) -> AppMetadata | None:
        url = self._build_search_url(package_name, version)
        logger.info(f"Navigating to search results endpoint for '{package_name}'...")
        self.page.goto(url)

        try:
            self.page.wait_for_selector(
                ".appRow, .variants-table, .downloadButton", timeout=15000
            )
        except Exception:
            logger.error("Timeout reached. No valid target layouts loaded.")
            return None

        current_html = self.page.content()
        parser = HTMLParser(current_html)

        # 1. We landed on a standard Search Results Page list
        if parser.css_first(".appRow"):
            logger.info(
                "Multiple results found. Extracting matching release row target..."
            )
            master_release_url = self._extract_first_valid_row(
                parser, version, only_release
            )
            if not master_release_url:
                logger.warning(
                    "No matching release rows survived filtering parameter constraints."
                )
                return None

            logger.info(f"Navigating to release page: {master_release_url}")
            self.page.goto(master_release_url)

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
                parser, preferred_arch, preferred_dpi, prefer_bundle
            )
            if not variant_page_url:
                logger.warning(
                    "Failed to calculate an eligible architectural match variant row target link."
                )
                return None

            logger.info(
                f"Navigating to isolated variant details page: {variant_page_url}"
            )
            self.page.goto(variant_page_url)
            try:
                self.page.wait_for_selector(".downloadButton", timeout=15000)
            except Exception:
                logger.error("Final download button missing from layout DOM context.")
                return None

            parser = HTMLParser(self.page.content())
        else:
            logger.info(
                "Single variant detected! Skipped variants grid directly to final download layout."
            )

        logger.info(
            "Successfully extracted complete validated package payload asset target info."
        )
        return self._extract_final_metadata(parser)

    def _extract_first_valid_row(
        self, parser, target_version, only_release
    ) -> str | None:
        app_rows = parser.css(".appRow")
        for row in app_rows:
            title_element = row.css_first("a.fontBlack")
            if not title_element:
                continue

            title_text = title_element.text(strip=True).lower()

            if not target_version and only_release:
                if any(x in title_text for x in ["alpha", "beta", "dev", "canary"]):
                    continue

            if target_version and target_version.lower() not in title_text:
                continue

            relative_url = title_element.attributes.get("href", "")
            return urljoin(self.BASE_URL, relative_url)

        return None

    def _get_best_variant(
        self, parser, preferred_arch, preferred_dpi, prefer_bundle
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
        except Exception as e:
            logger.warning(
                f"Standard chunk connection dropped: {e}. Falling back to browser..."
            )
            return self._download_binary_via_browser(target_url, dest_path)

    def _download_binary_via_browser(self, target_url, dest_path) -> str | None:
        logger.info(
            "Navigating to intermediate redirect page to capture explicit token..."
        )
        try:
            self.page.goto(target_url)

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
                    self.page.goto(absolute_file_url)
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

        except Exception as e:
            logger.error(f"Direct browser download extraction sequence failed: {e}")
            return None

    def close(self):
        logger.info(
            "Closing active cloaked browser environment process components cleanly."
        )
        self.browser.close()


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
