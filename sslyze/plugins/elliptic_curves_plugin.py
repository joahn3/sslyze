from concurrent.futures._base import Future
from dataclasses import dataclass
from typing import List, Optional

from nassl.ephemeral_key_info import OpenSslEcNidEnum, EcDhEphemeralKeyInfo, _OPENSSL_NID_TO_SECG_ANSI_X9_62
from nassl.ssl_client import ClientCertificateRequested, SslClient

from sslyze import ServerConnectivityInfo
from sslyze.errors import ServerRejectedTlsHandshake, TlsHandshakeTimedOut
from sslyze.plugins.plugin_base import (
    ScanCommandResult,
    ScanCommandCliConnector,
    ScanCommandImplementation,
    ScanCommandExtraArguments,
    ScanJob,
    ScanCommandWrongUsageError,
)
from sslyze.server_connectivity import enable_ecdh_cipher_suites


@dataclass(frozen=True)
class EllipticCurve:
    """A specific elliptic curve.

    Attributes:
        name: The ANSI X9.62 name if available, otherwise the SECG name.
        openssl_nid: The OpenSSL NID_XXX value valid for OpenSslEvpPkeyEnum.EC (obj_mac.h).
    """

    name: str
    openssl_nid: int


@dataclass(frozen=True)
class SupportedEllipticCurvesScanResult(ScanCommandResult):
    """The result of testing a server for supported elliptic curves.

    Attributes:
        supports_ecdh_key_exchange: True if the server supports at least one cipher suite with an ECDH key exchange.
        supported_curves: A list of `EllipticCurve` that were accepted by the server or `None` if the server does not
            support ECDH cipher suites.
        rejected_curves: A list of `EllipticCurve` that were rejected by the server or `None` if the server does not
            support ECDH cipher suites.
    """

    supports_ecdh_key_exchange: bool
    supported_curves: Optional[List[EllipticCurve]]
    rejected_curves: Optional[List[EllipticCurve]]


class _SupportedEllipticCurvesCliConnector(ScanCommandCliConnector[SupportedEllipticCurvesScanResult, None]):

    _cli_option = "elliptic_curves"
    _cli_description = "Test a server for supported elliptic curves."

    @classmethod
    def result_to_console_output(cls, result: SupportedEllipticCurvesScanResult) -> List[str]:
        result_as_txt = [cls._format_title("Elliptic Curve Key Exchange")]

        if not result.supports_ecdh_key_exchange:
            result_as_txt.append(
                cls._format_subtitle("The server does not support cipher suites with ECDH key exchanges.")
            )
        else:
            if result.supported_curves is None:
                raise RuntimeError("Should never happen")
            if result.rejected_curves is None:
                raise RuntimeError("Should never happen")

            supported_curves_names = [curve.name for curve in result.supported_curves]
            rejected_curves_names = [curve.name for curve in result.rejected_curves]
            result_as_txt.append(cls._format_field("Supported curves:", ", ".join(supported_curves_names)))
            result_as_txt.append(cls._format_field("Rejected curves:", ", ".join(rejected_curves_names)))
        return result_as_txt


class SupportedEllipticCurvesImplementation(ScanCommandImplementation[SupportedEllipticCurvesScanResult, None]):
    """Test a server for supported elliptic curves.
    """

    cli_connector_cls = _SupportedEllipticCurvesCliConnector

    @classmethod
    def scan_jobs_for_scan_command(
        cls, server_info: ServerConnectivityInfo, extra_arguments: Optional[ScanCommandExtraArguments] = None
    ) -> List[ScanJob]:
        if extra_arguments:
            raise ScanCommandWrongUsageError("This plugin does not take extra arguments")

        if not server_info.tls_probing_result.supports_ecdh_key_exchange:
            # Nothing to test: the server doesn't support EC key exchange
            return [ScanJob(function_to_call=_raise_elliptic_curve_not_supported, function_arguments=[])]

        # List of curves are in https://tools.ietf.org/html/rfc4492#section-5.1.1 and
        # https://tools.ietf.org/html/rfc8446#section-4.2.7
        return [
            ScanJob(function_to_call=_test_curve, function_arguments=[server_info, curve_nid])
            for curve_nid in OpenSslEcNidEnum
        ]

    @classmethod
    def result_for_completed_scan_jobs(
        cls, server_info: ServerConnectivityInfo, completed_scan_jobs: List[Future]
    ) -> SupportedEllipticCurvesScanResult:
        if len(completed_scan_jobs) < 1:
            raise RuntimeError(f"Unexpected number of scan jobs received: {completed_scan_jobs}")

        if len(completed_scan_jobs) == 1:
            try:
                completed_scan_jobs[0].result()
                raise RuntimeError("Should never happen")
            except _EllipticCurveNotSupported:
                return SupportedEllipticCurvesScanResult(
                    supports_ecdh_key_exchange=False, supported_curves=None, rejected_curves=None,
                )
        else:
            all_ecdh_results = [scan_job.result() for scan_job in completed_scan_jobs]
            return SupportedEllipticCurvesScanResult(
                supports_ecdh_key_exchange=True,
                supported_curves=[
                    ec_result.curve for ec_result in all_ecdh_results if ec_result.was_accepted_by_server
                ],
                rejected_curves=[
                    ec_result.curve for ec_result in all_ecdh_results if not ec_result.was_accepted_by_server
                ],
            )


class _EllipticCurveNotSupported(Exception):
    pass


def _raise_elliptic_curve_not_supported() -> None:
    raise _EllipticCurveNotSupported()


@dataclass(frozen=True)
class _EllipticCurveResult:
    curve: EllipticCurve
    was_accepted_by_server: bool


def _test_curve(server_info: ServerConnectivityInfo, curve_nid: OpenSslEcNidEnum) -> _EllipticCurveResult:
    if not server_info.tls_probing_result.supports_ecdh_key_exchange:
        raise RuntimeError("Should never happen")

    tls_version = server_info.tls_probing_result.highest_tls_version_supported
    ssl_connection = server_info.get_preconfigured_tls_connection(
        override_tls_version=tls_version, should_use_legacy_openssl=False
    )
    if not isinstance(ssl_connection.ssl_client, SslClient):
        raise RuntimeError(
            "Should never happen: specified should_use_legacy_openssl=False but didn't get the modern SSL client"
        )

    # Set curve to test whether it is supported by the server
    enable_ecdh_cipher_suites(tls_version, ssl_connection.ssl_client)
    ssl_connection.ssl_client.set_groups([curve_nid])

    try:
        ssl_connection.connect()
        negotiated_ephemeral_key = ssl_connection.ssl_client.get_ephemeral_key()

    # Error handling here mis similar to test_cipher_suite.py
    except ClientCertificateRequested:
        negotiated_ephemeral_key = ssl_connection.ssl_client.get_ephemeral_key()

    except (TlsHandshakeTimedOut, ServerRejectedTlsHandshake):
        negotiated_ephemeral_key = None

    finally:
        ssl_connection.close()

    # If no error occurred check if the curve was really used
    curve_name = _OPENSSL_NID_TO_SECG_ANSI_X9_62[curve_nid]  # TODO(AD): Make this public in nassl
    if negotiated_ephemeral_key:
        if isinstance(negotiated_ephemeral_key, EcDhEphemeralKeyInfo):
            if negotiated_ephemeral_key.curve != curve_nid:
                raise RuntimeError("Should never happen")

            return _EllipticCurveResult(
                curve=EllipticCurve(name=curve_name, openssl_nid=curve_nid.value), was_accepted_by_server=True,
            )

    return _EllipticCurveResult(
        curve=EllipticCurve(name=curve_name, openssl_nid=curve_nid.value), was_accepted_by_server=False,
    )
