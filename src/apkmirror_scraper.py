import logging
import os
import urllib.parse
from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field, ConfigDict
from cloakbrowser import launch

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("APKMirrorScraper")


# ==========================================
# SELECTION CONFIGURATION ENUMS
# ==========================================
class PackageType(str, Enum):
    APK = "APK"
    BUNDLE = "BUNDLE"


class Architecture(str, Enum):
    UNIVERSAL = "universal"
    ARM64_V8A = "arm64-v8a"
    ARMEABI_V7A = "armeabi-v7a"
    X86 = "x86"
    X86_64 = "x86_64"


class DisplayDensity(str, Enum):
    NODPI = "nodpi"
    DPI_120_480 = "120-480dpi"
    DPI_320_640 = "320-640dpi"
    DPI_160 = "160dpi"
    DPI_240 = "240dpi"
    DPI_320 = "320dpi"
    DPI_480 = "480dpi"
    DPI_640 = "640dpi"


# ==========================================
# PYDANTIC SELECTION CONFIGURATION MODEL
# ==========================================
class FilterOptions(BaseModel):
    model_config = ConfigDict(populate_by_name=True, use_enum_values=True)

    pkg_type: Optional[PackageType] = Field(default=None, alias="type")
    arch: Optional[Architecture] = Field(default=None)
    dpi: Optional[DisplayDensity] = Field(default=None)


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
        page_instance,
    ):
        self.app_name = app_name
        self.version = version
        self.package_name = package_name
        self.download_url = download_url
        self.icon_url = icon_url
        self.metadata = metadata
        self._page = page_instance

    def download(self, download_dir: str = "./downloads") -> Optional[str]:
        try:
            logger.info("Initializing background file preparation link layer...")
            os.makedirs(download_dir, exist_ok=True)

            logger.info("Direct-routing page layer into the file stream context...")

            with self._page.expect_download(timeout=90000) as download_info:
                try:
                    # If we are already on the gateway page, try to click the fallback link
                    # instead of re-navigating, which can sometimes be blocked or ignored.
                    if "?key=" in self._page.url and (
                        "?key=" in self.download_url
                        or self.download_url == self._page.url
                    ):
                        logger.info(
                            "Already on gateway page. Clicking fallback link to trigger download..."
                        )
                        fallback = self._page.locator(
                            "p:has-text('click here') a, a:has-text('here')"
                        ).first
                        if fallback.count() > 0:
                            fallback.click()
                        else:
                            # If no fallback link, just reload or goto
                            self._page.goto(self.download_url)
                    else:
                        self._page.goto(self.download_url)
                except Exception as e:
                    # Playwright often throws "Download is starting" error when navigating to a download link
                    if "Download is starting" in str(e):
                        logger.info(
                            "Detected download start signal from page interaction."
                        )
                    else:
                        raise e

            download = download_info.value
            filename = download.suggested_filename
            target_save_path = os.path.join(download_dir, filename)

            logger.info("File stream hook resolved: %s", filename)
            download.save_as(target_save_path)
            logger.info(
                "Successfully exported target asset wrapper to: %s", target_save_path
            )
            return target_save_path
        except Exception as e:
            logger.error("Download stream interaction aborted due to error: %s", e)
            self._page.screenshot(path="row_timeout_debug.png", full_page=True)
            return None

    def download_icon(self, download_dir: str = "./downloads") -> Optional[str]:
        if not self.icon_url:
            logger.warning("No icon URL found to download.")
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

            response = self._page.request.get(self.icon_url)
            if response.status == 200:
                with open(target_save_path, "wb") as file:
                    file.write(response.body())
                logger.info(
                    "Successfully saved application icon to: %s", target_save_path
                )
                return target_save_path
            else:
                logger.error(
                    "Failed to download icon, status code: %d", response.status
                )
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
    def __init__(self, headless: bool = True):
        self.browser = launch(headless=headless)
        self.context = self.browser.new_context(accept_downloads=True)
        self.page = self.context.new_page()

    def _handle_cookie_banner(self):
        selectors = [
            "text=ACCEPT ALL",
            "text=SAVE & EXIT",
            "button:has-text('ACCEPT ALL')",
            "button:has-text('SAVE & EXIT')",
            "text=Agree",
            "text=Consent",
            "button:has-text('Agree')",
            ".fc-button-label:has-text('Agree')",
        ]
        for selector in selectors:
            try:
                self.page.locator(selector).click(timeout=1500)
                logger.info(
                    "Successfully cleared cookies/consent banner matching: %s", selector
                )
                self.page.wait_for_timeout(800)
                break
            except Exception:
                continue

    def _normalize_version_for_url(self, version: str) -> str:
        # Strip leading 'v' if present (e.g. v1.2.3 -> 1.2.3)
        clean_v = version.lstrip("vV")
        return clean_v.replace(".", "-").replace(" ", "-").lower()

    def _find_release_page_link(
        self, package_name: str, target_version: Optional[str], org: Optional[str] = None, repo: Optional[str] = None
    ) -> Optional[str]:
        # 1. Direct Hub Navigation
        # ...
        
        # Clean target version (strip 'v')
        clean_version = target_version.lstrip("vV") if target_version else None

        if org and repo:
            hub_url = f"https://www.apkmirror.com/apk/{org}/{repo}/"
            logger.info("Direct navigating to application release hub: %s", hub_url)
            self.page.goto(hub_url)
            self._handle_cookie_banner()
            
            if clean_version:
                version_slug = self._normalize_version_for_url(clean_version)
                links = self.page.locator(".appRow a.fontBlack").all()
                for link in links:
                    href = link.get_attribute("href") or ""
                    # Check for slug with leading dash to avoid partial matches
                    if f"-{version_slug}-release/" in href:
                        logger.info("Found matching version link in hub: %s", href)
                        return href
            else:
                first_link = self.page.locator(".appRow a.fontBlack").first
                if first_link.count() > 0:
                    href = first_link.get_attribute("href") or ""
                    logger.info("Auto-routing to newest release in hub: %s", href)
                    return href

        # 2. Search Strategy
        # Try specific search first (Package + Version), then broad (Package only)
        search_queries = []
        if clean_version:
            search_queries.append(f"{package_name} {clean_version}")
        search_queries.append(package_name)

        version_slug = self._normalize_version_for_url(clean_version) if clean_version else None
        all_keywords = [k.lower() for k in package_name.split(".") if len(k) > 2]
        generic = {"google", "android", "apps", "inc", "com", "net", "org"}
        match_keywords = [k for k in all_keywords if k not in generic] or all_keywords

        for query in search_queries:
            url = f"https://www.apkmirror.com/?post_type=app_release&searchtype=apk&f=p&s={query}"
            logger.info("Searching APKMirror: %s", url)
            self.page.goto(url)
            self._handle_cookie_banner()

            # Check if we were redirected to a hub page (URL ends in org/repo/)
            # Hub URL usually looks like /apk/org/repo/ (6 segments)
            path_parts = [p for p in urllib.parse.urlparse(self.page.url).path.split("/") if p]
            if len(path_parts) == 3 and path_parts[0] == "apk":
                logger.info("Search redirected to application hub: %s", self.page.url)
                links = self.page.locator(".appRow a.fontBlack").all()
                for link in links:
                    href = link.get_attribute("href") or ""
                    if version_slug:
                         if f"-{version_slug}-release/" in href: return href
                    else:
                         return href
                continue # Try next query if hub didn't have version

            # Scan results
            links_locator = self.page.locator(".appRow a.fontBlack")
            try:
                # Wait for any result or the "no results" message
                self.page.wait_for_selector(".appRow a.fontBlack, p:has-text('No results found')", timeout=5000)
            except:
                pass

            rows = links_locator.all()
            if not rows:
                continue # Try next query

            for row in rows:
                href = row.get_attribute("href") or ""
                text = row.inner_text().lower()
                if "/apk/" in href and href.endswith("-release/"):
                    # RELEVANCE CHECK: Link or text must contain a match keyword
                    if not any(k in href.lower() or k in text for k in match_keywords):
                        continue
                    
                    if version_slug:
                        if f"-{version_slug}-release/" in href:
                            logger.info("Found version match in search results: %s", href)
                            return href
                    else:
                        logger.info("Found newest release in search results: %s", href)
                        return href
        
        return None

    def fetch_apk(
        self,
        package_name: str,
        version: Optional[str] = None,
        options: Optional[FilterOptions] = None,
        org: Optional[str] = None,
        repo: Optional[str] = None,
    ) -> Optional[APKRelease]:
        opts = options or FilterOptions()

        target_type = opts.pkg_type.upper() if opts.pkg_type else ""
        target_arch = opts.arch.lower() if opts.arch else ""
        target_dpi = opts.dpi.lower() if opts.dpi else ""

        release_path = self._find_release_page_link(package_name, version, org=org, repo=repo)
        if not release_path:
            logger.error(
                "Failed to parse matching parent release link for package identifier mapping."
            )
            return None

        target_variant_hub = f"https://www.apkmirror.com{release_path}"
        logger.info(
            "Direct navigating to release variant matrix configuration map: %s",
            target_variant_hub,
        )
        self.page.goto(target_variant_hub, wait_until="load")
        self._handle_cookie_banner()

        variant_href = None
        chosen_metadata = {}

        # 1. Check for variants table first (Release Hub page)
        variants_table = self.page.locator(".variants-table")
        if variants_table.count() > 0:
            logger.info("Variants table found. Filtering variants...")
            rows = variants_table.locator(".table-row").all()

            for index, row in enumerate(rows):
                # Skip header
                if (
                    "table-cell headerFont" in (row.get_attribute("class") or "")
                    or index == 0
                ):
                    continue

                cells = row.locator(".table-cell").all()
                if len(cells) < 5:
                    continue

                variant_text_block = cells[0].inner_text()
                arch_text = cells[1].inner_text().strip().lower()
                min_version_text = cells[2].inner_text().strip()
                dpi_text = cells[3].inner_text().strip().lower()

                pkg_type = "BUNDLE" if "BUNDLE" in variant_text_block.upper() else "APK"

                logger.info(
                    "Inspecting variant -> Type: %s, Arch: %s, DPI: %s",
                    pkg_type,
                    arch_text,
                    dpi_text,
                )

                if target_type and target_type != pkg_type:
                    continue
                if (
                    target_arch
                    and target_arch != arch_text
                    and target_arch not in arch_text
                ):
                    continue
                if target_dpi and target_dpi != dpi_text and target_dpi not in dpi_text:
                    continue

                anchor_link = cells[4].locator("a.accent_color").first
                if anchor_link.count() == 0:
                    anchor_link = cells[0].locator("a.accent_color").first

                variant_href = anchor_link.get_attribute("href")
                chosen_metadata = {
                    "type": pkg_type,
                    "arch": arch_text,
                    "min_android": min_version_text,
                    "dpi": dpi_text,
                }
                break

        # 2. If no variant selected or table missing, check if we are already on a variant/download page
        if not variant_href:
            logger.info(
                "No variant matched or table missing. Checking for direct download/variant buttons..."
            )

            # Look for big download buttons that might be on a single-variant release page
            # Labels like "Download APK", "Download Bundle", "Download APKM"
            # EXCLUDE "Premium" or "Subscription" links
            download_buttons = self.page.locator(
                "a.downloadButton, a:has-text('Download APK'), a.accent_bg"
            ).all()
            for btn in download_buttons:
                btn_href = btn.get_attribute("href") or ""
                btn_text = btn.inner_text().upper()

                if "PREMIUM" in btn_text or "SUBSCRIPTION" in btn_text:
                    continue

                # If it's a download button, it usually points to /download/
                if "DOWNLOAD" not in btn_text and "/download/" not in btn_href:
                    continue

                # Heuristic for type
                p_type = (
                    "BUNDLE"
                    if any(x in btn_text for x in ["BUNDLE", "APKM", "XAPK"])
                    else "APK"
                )

                if target_type and target_type != p_type:
                    continue

                variant_href = btn_href
                chosen_metadata["type"] = p_type
                logger.info(
                    "Found suitable direct download/variant link: %s", variant_href
                )
                break

            # If STILL no variant href, and we are on a release page, we might just need to extract the direct URL
            if not variant_href and "-release/" in self.page.url:
                logger.info("Assuming current page is the final variant page.")
                variant_href = self.page.url.replace("https://www.apkmirror.com", "")

        if not variant_href:
            logger.warning("Could not find suitable variant or download path.")
            return None

        # Navigate to the variant/download landing page
        final_download_landing = f"https://www.apkmirror.com{variant_href}"
        if self.page.url != final_download_landing:
            logger.info(
                "Navigating to final variant/download landing: %s",
                final_download_landing,
            )
            self.page.goto(final_download_landing, wait_until="load")
            self._handle_cookie_banner()

        # Check if we landed on a Premium pitch page
        if (
            "premium" in self.page.url.lower()
            or self.page.locator("text=Join APKMirror Premium").count() > 0
        ):
            logger.error(
                "Hit a Premium gate/pitch page. Download may be restricted or blocked."
            )
            return None

        # Icon extraction
        icon_url = None
        try:
            primary_img = self.page.locator("img#primaryimage").first
            if primary_img.count() > 0:
                src_attribute = primary_img.get_attribute("src") or ""
                if "ap_resize.php" in src_attribute and "src=" in src_attribute:
                    parsed_query = urllib.parse.parse_qs(
                        urllib.parse.urlparse(src_attribute).query
                    )
                    if "src" in parsed_query:
                        icon_url = parsed_query["src"][0]
                else:
                    icon_url = src_attribute
                if icon_url:
                    logger.info("Extracted icon URL: %s", icon_url)
        except:
            pass

        # Metadata Verification
        try:
            self.page.wait_for_selector(
                ".apk-detail-table, .appspec-table", timeout=5000
            )
            detail_text = self.page.locator(
                ".apk-detail-table, .appspec-table"
            ).first.inner_text()

            # Only do strict package matching if we actually found a table with "Package:" in it
            if (
                "Package:" in detail_text
                and f"Package: {package_name}" not in detail_text
            ):
                logger.error(
                    "Package mismatch! Expected %s but found different data. Detail text: %s",
                    package_name,
                    detail_text,
                )
                return None

            app_name, parsed_version = "Unknown", version or "Unknown"
            for line in detail_text.split("\n"):
                line = line.strip()
                if line.startswith("App:"):
                    app_name = line.split("App:", 1)[1].strip()
                elif line.startswith("Version:"):
                    parsed_version = line.split("Version:", 1)[1].strip()
                elif "Architecture:" in line and not chosen_metadata.get("arch"):
                    chosen_metadata["arch"] = (
                        line.split("Architecture:", 1)[1].strip().lower()
                    )
        except:
            logger.warning(
                "Could not find or parse APK detail table. Proceeding with basic metadata."
            )
            app_name = package_name
            parsed_version = version or "Unknown"

        # Find the final download gateway button
        # Usually id="download-button" or class="downloadButton"
        # On some pages it's just a link that says "Download APK"
        # On actual gateway pages (?key=...), it's a specific link
        # EXCLUDE "Premium" or "Subscription" links
        gateway_btns = self.page.locator(
            "a.downloadButton, a:has-text('Download APK'), a[data-google-vignette='false']"
        ).all()

        gateway_btn = None
        for btn in gateway_btns:
            btn_text = btn.inner_text().upper()
            if "PREMIUM" in btn_text or "SUBSCRIPTION" in btn_text:
                continue
            gateway_btn = btn
            break

        # If we are already on a ?key= gateway page, the download usually starts automatically,
        # but we want to grab the link just in case, or click the "here" link if it doesn't.
        if "?key=" in self.page.url:
            # Look for the link inside the "If the download doesn't start, click here" text
            fallback_link = self.page.locator(
                "p:has-text('click here') a, a:has-text('here')"
            ).first
            if fallback_link.count() > 0:
                gateway_btn = fallback_link

        if not gateway_btn or gateway_btn.count() == 0:
            logger.error("Could not find final download button on landing page.")
            return None

        button_href = gateway_btn.get_attribute("href") or ""
        # Handle relative vs absolute
        absolute_download_gateway = (
            button_href
            if button_href.startswith("http")
            else f"https://www.apkmirror.com{button_href}"
        )

        logger.info("Verification complete. Ready for download.")
        return APKRelease(
            app_name=app_name,
            version=parsed_version,
            package_name=package_name,
            download_url=absolute_download_gateway,
            icon_url=icon_url,
            metadata=chosen_metadata,
            page_instance=self.page,
        )

    def get_available_versions_from_hub(self, org: str, repo: str, limit: int = 10) -> list[str]:
        hub_url = f"https://www.apkmirror.com/apk/{org}/{repo}/"
        logger.info("Fetching available versions for %s from hub: %s", repo, hub_url)
        self.page.goto(hub_url)
        self._handle_cookie_banner()
        
        try:
            self.page.wait_for_selector(".appRow a.fontBlack", timeout=8000)
        except Exception:
            return []

        links = self.page.locator(".appRow a.fontBlack").all()
        versions = []
        import re
        
        for link in links:
            href = link.get_attribute("href") or ""
            text = link.inner_text().lower()

            if f"/{repo}-" in href and href.endswith("-release/"):
                match = re.search(r"(\d+\.[\d\.]+\d*)", text)
                if match:
                    v = match.group(1)
                    if v not in versions:
                        versions.append(v)
                        if len(versions) >= limit:
                            break
        return versions

    def get_available_versions(self, package_name: str, limit: int = 10, org: Optional[str] = None, repo: Optional[str] = None) -> list[str]:
        if org and repo:
            return self.get_available_versions_from_hub(org, repo, limit)
            
        base_search = f"https://www.apkmirror.com/?post_type=app_release&searchtype=apk&f=p&s={package_name}"
        logger.info(f"Fetching available versions for {package_name} from APKMirror...")

        # Keywords for relevance check
        all_keywords = [k.lower() for k in package_name.split(".") if len(k) > 2]
        generic = {"google", "android", "apps", "inc", "com", "net", "org"}
        specific_keywords = [k for k in all_keywords if k not in generic]
        match_keywords = specific_keywords if specific_keywords else all_keywords

        self.page.goto(base_search)
        self._handle_cookie_banner()

        try:
            self.page.wait_for_selector(".appRow a.fontBlack", timeout=8000)
        except Exception:
            return []

        links = self.page.locator(".appRow a.fontBlack").all()
        versions = []

        for link in links:
            href = link.get_attribute("href") or ""
            text = link.inner_text().lower()

            # Link format: /apk/vendor/app-name/app-name-1-2-3-release/
            if "/apk/" in href and href.endswith("-release/"):
                # RELEVANCE CHECK: Link or text must contain a match keyword
                if not any(k in href.lower() or k in text for k in match_keywords):
                    continue

                # Let's use a more robust version extraction from the text
                import re

                match = re.search(r"(\d+\.[\d\.]+\d*)", text)
                if match:
                    v = match.group(1)
                    if v not in versions:
                        versions.append(v)
                        if len(versions) >= limit:
                            break
        return versions

    def close(self):
        self.context.close()
        self.browser.close()


# ==========================================
# MAIN EXECUTION THREAD
# ==========================================
if __name__ == "__main__":
    scraper = APKMirrorScraper(headless=True)

    target_package = "com.google.android.youtube"
    requested_version = "20.47.62"

    filtering_matrix = FilterOptions(
        type=PackageType.BUNDLE, arch=Architecture.UNIVERSAL
    )

    release = scraper.fetch_apk(target_package, version=requested_version)

    if release:
        logger.info("=== Result Object Verified ===")
        logger.info("Entity Title:       %s", release.app_name)
        logger.info("Entity Version:     %s", release.version)
        logger.info("Target Gateway:     %s", release.download_url)
        logger.info("Branding Icon URL:  %s", release.icon_url)
        logger.info("==============================")

        # Download payload package
        release.download(download_dir="../youtube_packages")

        # Download clean primary high-res icon image asset folder
        release.download_icon(download_dir="../youtube_packages")
    else:
        logger.warning(
            "Main process loop resolved without finding an execution release entity target."
        )

    scraper.close()
