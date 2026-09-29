"""Radarr identity reads and narrowly scoped cancellation operations."""
from pathlib import Path

import secrets
from .arr_client import ArrClient, ArrError, Settings

CONFIG_PATH = Path.home() / ".config/cube/radarr.env"


class RadarrClient(ArrClient):
    service = "RADARR"
    config_path = CONFIG_PATH
    resource = "movie"

    def find_movie(self, tmdb_id):
        movies = self._request("GET", f"/movie?tmdbId={tmdb_id}", array=True)
        if not movies:
            return None
        if (len(movies) != 1 or movies[0].get("tmdbId") != tmdb_id
                or type(movies[0].get("id")) is not int or movies[0]["id"] <= 0
                or type(movies[0].get("hasFile")) is not bool
                or type(movies[0].get("monitored")) is not bool):
            raise ArrError("I couldn't identify the movie safely in Radarr.")
        return movies[0]

    def verify_server(self, servers, server_id):
        settings = Settings.load(self.config_path, self.service)
        candidates = [s for s in servers if isinstance(s, dict) and not s.get("is4k")
                      and (s.get("id") == server_id if server_id is not None else s.get("isDefault"))]
        if (len(candidates) != 1 or not isinstance(candidates[0].get("apiKey"), str)
                or not secrets.compare_digest(candidates[0]["apiKey"], settings.api_key)):
            raise ArrError("The request belongs to a different or unverified Radarr server.")

    def queue_for_movie(self, movie_id):
        records, page = [], 1
        while True:
            data = self._get(f"/queue?movieIds={movie_id}&page={page}&pageSize=100&includeMovie=true")
            items = data.get("records")
            total = data.get("totalRecords")
            if not isinstance(items, list) or type(total) is not int or total < 0:
                raise ArrError("Radarr returned an unexpected queue.")
            for item in items:
                if (not isinstance(item, dict) or item.get("movieId") != movie_id
                        or type(item.get("id")) is not int or item["id"] <= 0):
                    raise ArrError("I couldn't identify the download safely.")
            records.extend(items)
            if len(records) >= total:
                return records
            if not items or page >= 100:
                raise ArrError("I couldn't read the complete download queue.")
            page += 1

    def set_monitored(self, movie_id, monitored):
        self._request("PUT", "/movie/editor", array=True,
                      json={"movieIds": [movie_id], "monitored": monitored})

    def remove_queue_item(self, queue_id):
        self._request("DELETE", f"/queue/{queue_id}", allow_not_found=True,
                      params={"removeFromClient": "true", "blocklist": "false", "skipRedownload": "true"})

    def delete_movie(self, movie_id):
        # Queue cancellation removes partial data; never delete imported files here.
        if type(movie_id) is not int or movie_id <= 0:
            raise ArrError("Invalid movie identity.")
        self._request("DELETE", f"/movie/{movie_id}", allow_not_found=True,
                      params={"deleteFiles": "false", "addImportExclusion": "false"})
