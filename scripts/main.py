
import os
import re
import sys
import time
import pathlib
import argparse
import subprocess

import requests
from dotenv import load_dotenv


# ==========================================
# DIRECTORIES
# ==========================================

script_directory = pathlib.Path(__file__).parent.resolve()
root_directory = script_directory.parent.resolve()

env_file = root_directory / ".env"
subtitles_directory = root_directory / "subtitles"

load_dotenv(dotenv_path=env_file)

subtitles_directory.mkdir(parents=True, exist_ok=True)


# ==========================================
# CONFIGURATION
# ==========================================

API_URL = "https://api.opensubtitles.com/api/v1"

API_KEY = os.getenv("OPENSUBTITLES_API_KEY", "").strip()
USERNAME = os.getenv("OPENSUBTITLES_USERNAME", "").strip()
PASSWORD = os.getenv("OPENSUBTITLES_PASSWORD", "")

LANGUAGE = "en"
REQUEST_DELAY = 1.1


# ==========================================
# OPENSUBTITLES API
# ==========================================

class OpenSubtitles:

    def __init__(self):
        self.session = requests.Session()
        self.base_url = API_URL

        self.session.headers.update({
            "Api-Key": API_KEY,
            "User-Agent": "JellyfinSubtitleDownloader v1.0",
            "Accept": "application/json"
        })

    def request(self, method, endpoint, **kwargs):
        response = self.session.request(
            method,
            f"{self.base_url}/{endpoint}",
            timeout=30,
            **kwargs
        )

        if response.status_code == 429:
            raise RuntimeError(
                "OpenSubtitles rate limit reached."
            )

        if response.status_code in (401, 403):
            raise RuntimeError(
                f"Authentication failed: "
                f"{response.text[:200]}"
            )

        response.raise_for_status()
        return response.json()

    def login(self):
        if not USERNAME or not PASSWORD:
            print("Using API key without account login.")
            return

        data = self.request(
            "POST",
            "login",
            json={
                "username": USERNAME,
                "password": PASSWORD
            }
        )

        token = data.get("token")

        if not token:
            raise RuntimeError(
                "Login did not return an authentication token."
            )

        self.session.headers["Authorization"] = (
            f"Bearer {token}"
        )

        base_url = data.get("base_url")

        if base_url:
            if not base_url.startswith("https://"):
                base_url = "https://" + base_url

            parsed = requests.utils.urlparse(base_url)

            if parsed.hostname in {
                "api.opensubtitles.com",
                "vip-api.opensubtitles.com"
            }:
                self.base_url = (
                    f"https://{parsed.hostname}/api/v1"
                )

        print("Successfully logged in to OpenSubtitles.")

    def search(self, show, season, episode):
        data = self.request(
            "GET",
            "subtitles",
            params={
                "query": show,
                "season_number": season,
                "episode_number": episode,
                "languages": LANGUAGE,
                "type": "episode",
                "order_by": "download_count",
                "order_direction": "desc"
            }
        )

        results = []

        for result in data.get("data", []):
            attrs = result.get("attributes", {})
            feature = attrs.get("feature_details") or {}

            if attrs.get("language") != LANGUAGE:
                continue

            # Reject results for another episode.
            result_season = feature.get("season_number")
            result_episode = feature.get("episode_number")

            if (
                result_season is not None
                and int(result_season) != season
            ):
                continue

            if (
                result_episode is not None
                and int(result_episode) != episode
            ):
                continue

            if not attrs.get("files"):
                continue

            results.append(result)

        return results

    def download(self, file_id, destination):
        data = self.request(
            "POST",
            "download",
            json={
                "file_id": file_id,
                "sub_format": "srt"
            }
        )

        download_url = data.get("link")

        if not download_url:
            raise RuntimeError(
                "OpenSubtitles returned no download URL."
            )

        response = requests.get(
            download_url,
            timeout=60
        )
        response.raise_for_status()

        content = response.content

        # Validate that the result resembles SRT.
        text = content.decode(
            "utf-8-sig",
            errors="replace"
        )

        if not re.search(
            r"\d{2}:\d{2}:\d{2}[,.]\d{3}",
            text
        ):
            raise ValueError(
                "Downloaded content is not valid SRT."
            )

        destination.parent.mkdir(
            parents=True,
            exist_ok=True
        )

        temporary = destination.with_suffix(".tmp")

        try:
            temporary.write_text(
                text,
                encoding="utf-8"
            )
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)

        return data.get("remaining")


# ==========================================
# HELPER FUNCTIONS
# ==========================================

def safe_name(name):
    return re.sub(
        r'[<>:"/\\|?*]',
        "",
        name
    ).strip(" .")


def parse_episodes(value):
    """
    Accepted formats:
        1
        1-7
        1,3,5
        1-3,7,9
    """

    episodes = set()

    for part in value.replace(" ", "").split(","):

        if not part:
            raise ValueError("Invalid episode input.")

        if "-" in part:
            start, end = map(
                int,
                part.split("-", 1)
            )

            if start < 1 or end < start:
                raise ValueError(
                    "Invalid episode range."
                )

            episodes.update(
                range(start, end + 1)
            )

        else:
            episode = int(part)

            if episode < 1:
                raise ValueError(
                    "Episode numbers must be positive."
                )

            episodes.add(episode)

    return sorted(episodes)


def choose_subtitle(results):
    """
    Display up to five matching subtitle releases.
    The user can select a release or skip.
    """

    if not results:
        return None

    choices = results[:5]

    print("\nAvailable English subtitles:")

    for index, result in enumerate(choices, 1):
        attrs = result.get("attributes", {})

        release = attrs.get(
            "release",
            "Unknown release"
        )

        downloads = attrs.get(
            "download_count",
            0
        )

        print(
            f"  {index}. {release} "
            f"({downloads} downloads)"
        )

    selection = input(
        "\nChoose subtitle [1], or 's' to skip: "
    ).strip().lower()

    if selection == "s":
        return None

    if not selection:
        selection = "1"

    if not selection.isdigit():
        print("Invalid selection.")
        return None

    index = int(selection) - 1

    if index < 0 or index >= len(choices):
        print("Invalid selection.")
        return None

    files = choices[index]["attributes"]["files"]

    return files[0]["file_id"]


# ==========================================
# MAIN
# ==========================================

def main():

    parser = argparse.ArgumentParser(
        description=(
            "Download English subtitles for "
            "TV shows using OpenSubtitles."
        )
    )

    parser.add_argument("--show", type=str)
    parser.add_argument("--season", type=int)
    parser.add_argument("--episodes", type=str)

    args = parser.parse_args()

    print("=" * 50)
    print("          JELLYFIN SUBTITLE DOWNLOADER")
    print("=" * 50)

    # Check .env configuration.
    if not env_file.is_file():
        print(f"\nError: .env file not found: {env_file}")
        return

    if not API_KEY:
        print(
            "\nError: OPENSUBTITLES_API_KEY "
            "is missing from root/.env"
        )
        return

    # Show name
    show = args.show or input(
        "\nEnter TV show name: "
    ).strip()

    if not show:
        print("Error: Show name cannot be empty.")
        return

    # Season number
    season = args.season

    if season is None:
        season = int(
            input("Enter season number: ")
        )

    if season < 0:
        print("Error: Invalid season number.")
        return

    # Episode numbers
    episode_input = args.episodes or input(
        "Enter episodes (e.g. 1-10 or 1,3,5): "
    ).strip()

    episodes = parse_episodes(episode_input)

    show_name = safe_name(show)

    if not show_name:
        print("Error: Invalid show name.")
        return

    # Output directory
    season_directory = (
        subtitles_directory
        / show_name
        / f"Season {season:02d}"
    )

    season_directory.mkdir(
        parents=True,
        exist_ok=True
    )

    print("\n" + "=" * 50)
    print("DOWNLOAD SETTINGS")
    print("=" * 50)
    print(f"Show       : {show}")
    print(f"Season     : {season}")
    print(f"Episodes   : {episodes}")
    print(f"Language   : English")
    print(f"Output     : {season_directory}")
    print("=" * 50)

    api = OpenSubtitles()
    api.login()

    downloaded = 0
    skipped = 0
    failed = 0

    for index, episode in enumerate(episodes, 1):

        episode_name = (
            f"{show_name} - "
            f"S{season:02d}E{episode:02d}"
        )

        destination = (
            season_directory
            / f"{episode_name}.en.srt"
        )

        print(
            f"\n[{index}/{len(episodes)}] "
            f"{episode_name}"
        )

        if destination.exists():
            print("  Subtitle already exists. Skipping.")
            skipped += 1
            continue

        try:
            results = api.search(
                show,
                season,
                episode
            )

            if not results:
                print("  No English subtitles found.")
                failed += 1
                continue

            file_id = choose_subtitle(results)

            if file_id is None:
                print("  Skipped.")
                skipped += 1
                continue

            remaining = api.download(
                file_id,
                destination
            )

            print(
                f"  Downloaded: {destination.name}"
            )

            downloaded += 1

            if remaining is not None:
                print(
                    f"  Remaining downloads: {remaining}"
                )

                if int(remaining) <= 0:
                    print(
                        "\nDaily download limit reached."
                    )
                    break

        except requests.RequestException as error:
            print(f"  Network error: {error}")
            failed += 1

        except (RuntimeError, ValueError) as error:
            print(f"  Error: {error}")
            failed += 1

            if any(
                word in str(error).lower()
                for word in (
                    "rate limit",
                    "authentication failed"
                )
            ):
                break

        time.sleep(REQUEST_DELAY)

    print("\n" + "=" * 50)
    print("DOWNLOAD SUMMARY")
    print("=" * 50)
    print(f"Downloaded : {downloaded}")
    print(f"Skipped    : {skipped}")
    print(f"Failed     : {failed}")
    print(f"Directory  : {season_directory}")
    print("=" * 50)


# ==========================================
# ENTRY POINT
# ==========================================

if __name__ == "__main__":

    subprocess.call(
        "clear" if os.name == "posix" else "cls",
        shell=True
    )

    try:
        main()

    except KeyboardInterrupt:
        print("\nDownload cancelled.")
        sys.exit(130)

    except (
        ValueError,
        RuntimeError,
        requests.RequestException
    ) as error:
        print(f"\nError: {error}")
        sys.exit(1)
