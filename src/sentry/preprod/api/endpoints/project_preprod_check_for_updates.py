import logging

from packaging.version import parse as parse_version
from pydantic import BaseModel
from rest_framework.request import Request
from rest_framework.response import Response

from sentry.api.api_owners import ApiOwner
from sentry.api.api_publish_status import ApiPublishStatus
from sentry.api.base import region_silo_endpoint
from sentry.api.bases.project import ProjectEndpoint, ProjectReleasePermission
from sentry.preprod.build_distribution_utils import (
    get_download_url_for_artifact,
    is_installable_artifact,
)
from sentry.preprod.models import PreprodArtifact
from sentry.types.ratelimit import RateLimit, RateLimitCategory

logger = logging.getLogger(__name__)


class InstallableBuildDetails(BaseModel):
    build_version: str
    build_number: int
    download_url: str
    app_name: str
    created_date: str


class CheckForUpdatesApiResponse(BaseModel):
    update: InstallableBuildDetails | None = None
    current: InstallableBuildDetails | None = None


@region_silo_endpoint
class ProjectPreprodArtifactCheckForUpdatesEndpoint(ProjectEndpoint):
    owner = ApiOwner.EMERGE_TOOLS
    publish_status = {
        "GET": ApiPublishStatus.EXPERIMENTAL,
    }
    permission_classes = (ProjectReleasePermission,)

    enforce_rate_limit = True
    rate_limits = {
        "GET": {
            RateLimitCategory.ORGANIZATION: RateLimit(
                limit=100, window=60
            ),  # 100 requests per minute per org
        }
    }

    def get(self, request: Request, project) -> Response:
        """
        Check for updates for a preprod artifact
        """
        main_binary_identifier = request.GET.get("main_binary_identifier")
        app_id = request.GET.get("app_id")

        platform = request.GET.get("platform")
        provided_version = request.GET.get("version")
        provided_build_configuration = request.GET.get("build_configuration")

        if not app_id or not platform or not provided_version:
            return Response({"error": "Missing required parameters"}, status=400)

        current = None
        update = None

        # Common filter logic
        def get_base_filters():
            filter_kwargs = {
                "project": project,
                "app_id": app_id,
            }

            if platform == "android":
                filter_kwargs["artifact_type__in"] = ["AAB", "APK"]
            elif platform == "ios":
                filter_kwargs["artifact_type"] = "XCARCHIVE"

            return filter_kwargs

        if main_binary_identifier:
            try:
                filter_kwargs = get_base_filters()
                filter_kwargs.update(
                    {
                        "main_binary_identifier": main_binary_identifier,
                        "build_version": provided_version,
                    }
                )

                if provided_build_configuration:
                    filter_kwargs["build_configuration"] = provided_build_configuration

                preprod_artifact = PreprodArtifact.objects.filter(**filter_kwargs).latest(
                    "date_added"
                )
            except PreprodArtifact.DoesNotExist:
                return Response({"error": "Not found"}, status=404)

            if preprod_artifact.build_version and preprod_artifact.build_number:
                current = InstallableBuildDetails(
                    build_version=preprod_artifact.build_version,
                    build_number=preprod_artifact.build_number,
                    app_name=preprod_artifact.app_name,
                    download_url=get_download_url_for_artifact(preprod_artifact, request),
                    created_date=preprod_artifact.date_added.isoformat(),
                )

        # Get the update object - find the highest version available
        # Get all build versions for this app and platform
        filter_kwargs = get_base_filters()
        all_versions = (
            PreprodArtifact.objects.filter(**filter_kwargs)
            .values_list("build_version", flat=True)
            .distinct()
        )

        # Find the highest semver version
        highest_version = None
        for version in all_versions:
            if version:
                try:
                    parsed_version = parse_version(version)
                    if highest_version is None or parsed_version > parse_version(highest_version):
                        highest_version = version
                except Exception:
                    # Skip invalid version strings
                    continue

        # Get all artifacts for the highest version
        if highest_version:
            filter_kwargs = get_base_filters()
            filter_kwargs["build_version"] = highest_version
            potential_artifacts = PreprodArtifact.objects.filter(**filter_kwargs)

            # Filter for installable artifacts and get the one with highest build_number
            installable_artifacts = [
                artifact for artifact in potential_artifacts if is_installable_artifact(artifact)
            ]

            if installable_artifacts:
                # Get the artifact with the highest build_number
                best_artifact = max(installable_artifacts, key=lambda x: x.build_number or 0)
                if best_artifact.build_version and best_artifact.build_number:
                    update = InstallableBuildDetails(
                        build_version=best_artifact.build_version,
                        build_number=best_artifact.build_number,
                        app_name=best_artifact.app_name,
                        download_url=get_download_url_for_artifact(best_artifact, request),
                        created_date=best_artifact.date_added.isoformat(),
                    )

        return Response(CheckForUpdatesApiResponse(current=current, update=update).dict())
