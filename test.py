import logging
import os
import re
import sys
import urllib.parse
from enum import Enum
from typing import Optional, Any
from pydantic import BaseModel, Field, ConfigDict
from rnet import BlockingClient, Impersonate
from selectolax.parser import HTMLParser

# Configure logging to go to STDOUT so it's captured by the build logs
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger("APKMirrorScraper")


# ==========================================
# SELECTION CONFIGURATION ENUMS
# ==========================================
class PackageType(str, Enum):
    APK = "apk_files"
    BUNDLE = "apkm_bundles"


class Architecture(str, Enum):
    UNIVERSAL = "universal"
    ARM64_V8A = "arm64-v8a"
    ARMEABI = "armeabi"
    ARMEABI_V7A = "armeabi-v7a"
    MIPS = "mips"
    MIPS64 = "mips64"
    X86 = "x86"
    X86_64 = "x86_64"


class DisplayDensity(str, Enum):
    NODPI = "nodpi"
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


# ==========================================
# PYDANTIC SELECTION CONFIGURATION MODEL
# ==========================================
class FilterOptions(BaseModel):
    model_config = ConfigDict(populate_by_name=True, use_enum_values=True)

    pkg_type: Optional[PackageType] = Field(default=None, alias="type")
    arch: Optional[list[Architecture]] = Field(default=None)
    dpi: Optional[list[DisplayDensity]] = Field(default=None)
    min_api: Optional[int] = Field(default=None, alias="minapi")


# ==========================================
# METADATA ENCAPSULATION WRAPPER
# ==========================================
class APKRelease:
    def __init__(
        self,
        app_name: str,
        version: str,
        package_name: str,
        download_url: str,
        icon_url: Optional[str],
        metadata: dict,
        client_instance: BlockingClient,
        org: Optional[str] = None,
        repo: Optional[str] = None,
    ):
        self.app_name = app_name
        self.version = version
        self.package_name = package_name
        self.download_url = download_url
        self.icon_url = icon_url
        self.metadata = metadata
        self.org = org
        self.repo = repo
        self._client = client_instance

    def download(self, download_dir: str = "./downloads") -> Optional[str]:
        try:
            logger.info("Initializing background file preparation link layer...")
            os.makedirs(download_dir, exist_ok=True)

            if "premium" in self.download_url.lower():
                logger.error(
                    "Download URL points to Premium gate: %s. Aborting.",
                    self.download_url,
                )
                return None

            logger.info("Direct-routing HTTP layer into the file stream context...")

            response = self._client.get(self.download_url, allow_redirects=True)
            if response.status != 200:
                logger.error(
                    "Failed to reach download gateway page. Status: %s",
                    response.status,
                )
                return None

            tree = HTMLParser(response.text())
            final_download_url = self.download_url

            # Avoid :contains selector as it can cause segmentation faults in selectolax
            fallback_node = None
            for node in tree.css("p a, a"):
                node_text = node.text(deep=True).lower()
                if "click here" in node_text or "here" == node_text.strip():
                    fallback_node = node
                    break

            if fallback_node:
                href = fallback_node.attributes.get("href")
                if href:
                    final_download_url = urllib.parse.urljoin(
                        "https://www.apkmirror.com", href
                    )
                    logger.info(
                        "Using extracted explicit raw stream URL: %s",
                        final_download_url,
                    )

            file_response = self._client.get(final_download_url, allow_redirects=True)
            if file_response.status != 200:
                logger.error(
                    "File download request rejected by server. Status: %s",
                    file_response.status,
                )
                return None

            cd_header_raw = file_response.headers.get("content-disposition", b"")
            cd_header = (
                cd_header_raw.decode("utf-8", "ignore")
                if isinstance(cd_header_raw, bytes)
                else ""
            )
            filename_match = re.search(r'filename=["\']?([^"\';]+)', cd_header)
            if filename_match:
                filename = filename_match.group(1)
            else:
                ext = "apkm" if self.metadata.get("type") == "BUNDLE" else "apk"
                filename = f"{self.package_name}_{self.version}.{ext}"

            target_save_path = os.path.join(download_dir, filename)
            logger.info("File stream hook resolved: %s", filename)

            with open(target_save_path, "wb") as f:
                f.write(file_response.bytes())

            logger.info(
                "Successfully exported target asset wrapper to: %s", target_save_path
            )
            return target_save_path
        except Exception as e:
            logger.error("Download stream interaction aborted due to error: %s", e)
            return None

    def download_icon(self, download_dir: str = "./downloads") -> Optional[str]:
        if not self.icon_url:
            return None
        try:
            logger.info("Downloading app icon asset...")
            os.makedirs(download_dir, exist_ok=True)

            ext = "png"
            if ".jpg" in self.icon_url.lower() or ".jpeg" in self.icon_url.lower():
                ext = "jpg"
            elif ".svg" in self.icon_url.lower():
                ext = "svg"

            filename = f"{self.package_name}_icon.{ext}"
            target_save_path = os.path.join(download_dir, filename)

            response = self._client.get(self.icon_url, allow_redirects=True)
            if response.status == 200:
                with open(target_save_path, "wb") as file:
                    file.write(response.bytes())
                return target_save_path
            return None
        except Exception as e:
            logger.error("Error saving app icon: %s", e)
            return None

    def __repr__(self):
        return f"<APKRelease App='{self.app_name}' Version='{self.version}' VariantData={self.metadata}>"


# ==========================================
# CORE CRAWLER CONTROLLER LAYER
# ==========================================
class APKMirrorScraper:
    def __init__(self):
        self.client = BlockingClient(
            impersonate=Impersonate.Firefox139,
        )
        # Using insert() as HeaderMap doesn't support update()
        # headers = {
        #     "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        #     "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        #     "Accept-Language": "en-US,en;q=0.9",
        #     "Accept-Encoding": "gzip, deflate",
        #     "DNT": "1",
        #     "Connection": "keep-alive",
        #     "Upgrade-Insecure-Requests": "1",
        #     "Sec-Fetch-Dest": "document",
        #     "Sec-Fetch-Mode": "navigate",
        #     "Sec-Fetch-Site": "none",
        #     "Sec-Fetch-User": "?1",
        # }
        # for k, v in headers.items():
        #     self.client.headers.insert(k, v)

    def _build_search_url(
        self,
        package_name: str,
        version: Optional[str] = None,
        options: Optional[FilterOptions] = None,
    ) -> str:
        # Use literal phrases with quotes to prevent automatic wildcards
        search_query = f'"{package_name}"'
        if version:
            search_query += f' "{version}"'

        params: dict[str, Any] = {
            "post_type": "app_release",
            "searchtype": "apk",
            "s": search_query,
        }

        opts = options or FilterOptions()

        if opts.min_api:
            params["minapi-min"] = str(opts.min_api)

        if opts.arch:
            params["arch[]"] = opts.arch

        if opts.dpi:
            params["dpi[]"] = opts.dpi

        if opts.pkg_type:
            params["bundles[]"] = [opts.pkg_type]
        else:
            # Default to both if not specified to maximize discovery
            params["bundles[]"] = ["apk_files", "apkm_bundles"]

        base_url = "https://www.apkmirror.com/"
        query_string = urllib.parse.urlencode(params, doseq=True)
        return f"{base_url}?{query_string}"

    def get_available_versions(
        self,
        package_name: str,
        limit: int = 10,
        options: Optional[FilterOptions] = None,
    ) -> dict:
        url = self._build_search_url(package_name, options=options)
        logger.info(
            "Fetching versions for %s from advanced search: %s", package_name, url
        )
        try:
            res = self.client.get(url, allow_redirects=True)
            redirected_url = res.url if hasattr(res, "url") else url
            logger.info("Final URL: %s | Status: %d", redirected_url, res.status)

            # logger.info("Body Snippet: %s", res.text()[:1000])

            if res.status != 200:
                return {"versions": [], "org": None, "repo": None}

            tree = HTMLParser(res.text())

            # If redirected directly to a release page
            if "/apk/" in redirected_url and redirected_url.endswith("-release/"):
                logger.info("Directly redirected to release page: %s", redirected_url)
                # Extract versions from this page (usually just one)
                match = re.search(r"(\d+\.[\d\.]+\d*)", redirected_url)
                if match:
                    d_org, d_repo = None, None
                    parts = [p for p in redirected_url.split("/") if p]
                    for i, part in enumerate(parts):
                        if part == "apk" and len(parts) > i + 2:
                            d_org, d_repo = parts[i + 1], parts[i + 2]
                            break
                    return {"versions": [match.group(1)], "org": d_org, "repo": d_repo}

            # Discovery releases using the stable broad pattern
            all_apk_links = [
                link.attributes.get("href") or ""
                for link in tree.css("a")
                if "/apk/" in (link.attributes.get("href") or "")
            ]
            logger.info("DEBUG: Found %d links with /apk/", len(all_apk_links))
            if all_apk_links:
                logger.info("DEBUG: First 5 /apk/ links: %s", all_apk_links[:5])

            links = [
                link
                for link in tree.css("a")
                if "/apk/" in (link.attributes.get("href") or "")
                and (link.attributes.get("href") or "").endswith("-release/")
            ]

            versions = []
            d_org, d_repo = None, None

            for link in links:
                if len(versions) >= limit:
                    break

                href = link.attributes.get("href") or ""
                text = link.text(deep=True).lower()

                # Basic validation that the link belongs to the package context
                if not d_org:
                    parts = [p for p in href.split("/") if p]
                    for i, part in enumerate(parts):
                        if part == "apk" and len(parts) > i + 2:
                            d_org, d_repo = parts[i + 1], parts[i + 2]
                            break

                match = re.search(r"(\d+\.[\d\.]+\d*)", text)
                if match:
                    v = match.group(1)
                    if v not in versions:
                        versions.append(v)

            return {"versions": versions, "org": d_org, "repo": d_repo}
        except Exception as e:
            logger.error("Advanced search version fetch failed: %s", e)
            return {"versions": [], "org": None, "repo": None}

    def fetch_apk(
        self,
        package_name: str,
        version: Optional[str] = None,
        options: Optional[FilterOptions] = None,
    ) -> Optional[APKRelease]:
        url = self._build_search_url(package_name, version=version, options=options)
        logger.info(
            "Discovering release for %s via advanced search: %s", package_name, url
        )

        try:
            res = self.client.get(url, allow_redirects=True)
            if res.status != 200:
                return None

            redirected_url = res.url if hasattr(res, "url") else url
            tree = HTMLParser(res.text())

            release_path = None
            d_org, d_repo = None, None

            # Look for release links
            links = [
                link
                for link in tree.css("a")
                if "/apk/" in (link.attributes.get("href") or "")
                and (link.attributes.get("href") or "").endswith("-release/")
            ]

            for link in links:
                href = link.attributes.get("href") or ""
                text = link.text(deep=True).lower()

                # Verify version match if specified
                if version:
                    if version in text or version.replace(".", "-") in href:
                        release_path = href
                        break
                else:
                    # Take first one
                    release_path = href
                    break

            if not release_path:
                # Check if we were redirected directly to the release page
                if "/apk/" in redirected_url and redirected_url.endswith("-release/"):
                    release_path = redirected_url
                else:
                    logger.warning(
                        "No release path found for %s %s", package_name, version
                    )
                    return None

            # Extract org/repo from release path
            parts = [p for p in release_path.split("/") if p]
            for i, part in enumerate(parts):
                if part == "apk" and len(parts) > i + 2:
                    d_org, d_repo = parts[i + 1], parts[i + 2]
                    break

        except Exception as e:
            logger.error("Error discovering release page for %s: %s", package_name, e)
            return None

        # Now navigate to the release matrix/variant page
        hub_url = urllib.parse.urljoin("https://www.apkmirror.com", release_path)
        logger.info("Navigating to release matrix: %s", hub_url)
        try:
            res = self.client.get(hub_url, allow_redirects=True)
            if res.status != 200:
                return None
            tree = HTMLParser(res.text())
        except Exception as e:
            logger.error("Failed to load release matrix page %s: %s", hub_url, e)
            return None

        variant_href = None
        chosen_metadata = {}

        # Filter Options for internal extraction
        opts = options or FilterOptions()
        target_type = "BUNDLE" if opts.pkg_type == PackageType.BUNDLE else "APK"

        target_archs = [
            a.value if isinstance(a, Architecture) else a for a in (opts.arch or [])
        ]
        target_dpis = [
            d.value if isinstance(d, DisplayDensity) else d for d in (opts.dpi or [])
        ]

        variants_table = tree.css_first(".variants-table")
        if variants_table:
            logger.info("Variants table found. Filtering...")
            rows = variants_table.css(".table-row")
            for index, row in enumerate(rows):
                row_class = row.attributes.get("class") or ""
                if "table-cell headerFont" in row_class or index == 0:
                    continue
                cells = row.css(".table-cell")
                if len(cells) < 5:
                    continue

                v_text = cells[0].text(deep=True)
                arch = cells[1].text(deep=True).strip().lower()
                dpi = cells[3].text(deep=True).strip().lower()
                p_type = "BUNDLE" if "BUNDLE" in v_text.upper() else "APK"

                if opts.pkg_type and target_type != p_type:
                    continue
                if target_archs and not any(ta in arch for ta in target_archs):
                    continue
                if target_dpis and not any(td in dpi for td in target_dpis):
                    continue

                # Find variant anchor
                anchor = None
                for a in cells[4].css("a") + cells[0].css("a"):
                    if "accent_color" in (a.attributes.get("class") or ""):
                        anchor = a
                        break

                if not anchor:
                    continue

                variant_href = anchor.attributes.get("href")
                if "premium" in (variant_href or "").lower():
                    variant_href = None
                    continue

                chosen_metadata = {"type": p_type, "arch": arch, "dpi": dpi}
                break

        if not variant_href:
            logger.info("Checking for direct download buttons...")
            btns = tree.css("a")
            for btn in btns:
                b_class = btn.attributes.get("class") or ""
                b_href = btn.attributes.get("href") or ""
                b_text = btn.text(deep=True).upper()

                is_dl_btn = (
                    "downloadButton" in b_class
                    or "accent_bg" in b_class
                    or "Download APK" in b_text
                )
                if not is_dl_btn:
                    continue

                if (
                    "PREMIUM" in b_text
                    or "SUBSCRIPTION" in b_text
                    or "PREMIUM" in b_href.upper()
                ):
                    continue
                if "DOWNLOAD" not in b_text and "/download/" not in b_href:
                    continue
                p_type = (
                    "BUNDLE"
                    if any(x in b_text for x in ["BUNDLE", "APKM", "XAPK"])
                    else "APK"
                )
                if opts.pkg_type and target_type != p_type:
                    continue
                variant_href = b_href
                chosen_metadata["type"] = p_type
                break

        if not variant_href:
            return None

        dl_landing = urllib.parse.urljoin("https://www.apkmirror.com", variant_href)
        try:
            res = self.client.get(dl_landing, allow_redirects=True)
            if res.status != 200:
                return None
            tree = HTMLParser(res.text())
        except Exception as e:
            logger.error("Failed to load download landing page %s: %s", dl_landing, e)
            return None

        hit_premium = False
        for text_node in tree.css("text"):
            if "Join APKMirror Premium" in text_node.text(deep=True):
                hit_premium = True
                break

        if "premium" in dl_landing.lower() or hit_premium:
            logger.error("Hit Premium gate at %s", dl_landing)
            return None

        icon_url = None
        img = tree.css_first("img#primaryimage")
        if img:
            src = img.attributes.get("src") or ""
            if "ap_resize.php" in src and "src=" in src:
                parsed = urllib.parse.parse_qs(urllib.parse.urlparse(src).query)
                if "src" in parsed:
                    icon_url = parsed["src"][0]
            else:
                icon_url = src

        detail_table = tree.css_first(".apk-detail-table, .appspec-table")
        if detail_table:
            detail_text = detail_table.text(deep=True)
            app_name, parsed_version = "Unknown", version or "Unknown"
            for line in detail_text.split("\n"):
                line = line.strip()
                if "App:" in line:
                    app_name = line.split("App:", 1)[1].strip()
                elif "Version:" in line:
                    parsed_version = line.split("Version:", 1)[1].strip()
                elif "Architecture:" in line and not chosen_metadata.get("arch"):
                    chosen_metadata["arch"] = (
                        line.split("Architecture:", 1)[1].strip().lower()
                    )

            # Clean up version string (remove excessive whitespace/newlines)
            parsed_version = " ".join(parsed_version.split())
        else:
            app_name, parsed_version = package_name, version or "Unknown"

        # Fallback for App Name if not found in table
        if app_name == "Unknown":
            h1 = tree.css_first("h1")
            if h1:
                # Usually "App Name Version"
                h1_text = h1.text(deep=True).strip()
                if version and version in h1_text:
                    app_name = h1_text.split(version)[0].strip()
                else:
                    app_name = h1_text
            else:
                app_name = package_name

        gateway_btns = tree.css("a")
        gateway_btn_href = None
        for btn in gateway_btns:
            b_class = btn.attributes.get("class") or ""
            b_href = btn.attributes.get("href") or ""
            b_text = btn.text(deep=True).upper()

            is_dl_btn = (
                "downloadButton" in b_class
                or "Download APK" in b_text
                or btn.attributes.get("data-google-vignette") == "false"
            )
            if not is_dl_btn:
                continue

            if (
                "PREMIUM" in b_text
                or "SUBSCRIPTION" in b_text
                or "PREMIUM" in b_href.upper()
            ):
                continue
            gateway_btn_href = b_href
            break

        if not gateway_btn_href:
            return None

        if "premium" in gateway_btn_href.lower():
            logger.error("Final button is a Premium link: %s", gateway_btn_href)
            return None

        abs_gateway = urllib.parse.urljoin(
            "https://www.apkmirror.com", gateway_btn_href
        )

        return APKRelease(
            app_name=app_name,
            version=parsed_version,
            package_name=package_name,
            download_url=abs_gateway,
            icon_url=icon_url,
            metadata=chosen_metadata,
            client_instance=self.client,
            org=d_org,
            repo=d_repo,
        )

    def close(self):
        pass


if __name__ == "__main__":
    scraper = APKMirrorScraper()

    # Test 1: Version Discovery
    pkg = "com.instagram.android"
    print(f"\n--- Testing Version Discovery for {pkg} ---")
    data = scraper.get_available_versions(pkg, limit=5)
    print(f"Discovered versions: {data.get('versions')}")

    # Test 2: Precise APK Fetching
    target_version = "426.0.0.37.68"
    print(f"\n--- Testing fetch_apk for {pkg} version {target_version} ---")

    # Example using advanced filters
    options = FilterOptions(
        arch=[Architecture.ARM64_V8A, Architecture.UNIVERSAL], min_api=26
    )

    release = scraper.fetch_apk(pkg, version=target_version, options=options)

    print(f"Fetch Result: {release}")
    if release:
        print(f"App Name: {release.app_name}")
        print(f"Package: {release.package_name}")
        print(f"Download Gateway: {release.download_url}")
        print(f"Metadata: {release.metadata}")
        release.download()
    else:
        print("Fetch failed, no release object returned.")
