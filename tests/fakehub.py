"""A simulated InfoMentor Hub, so tests run the real pupil switching and parsing code."""

import re

import httpx

HUB = "https://hub.infomentor.se"


class FakeHub:
    """Serve Hub's start page, pupil switcher and data endpoints through pytest-httpx.

    Like the real Hub, one pupil is selected per session. Data handlers receive the request
    and the pupil selected when it arrived, and every data read is logged as (path, pupil).
    """

    def __init__(self, httpx_mock, children=None, selected="second"):
        self.httpx_mock = httpx_mock
        self.children = dict(children or {"first": "One, Ada", "second": "Two, Bo"})
        self.selected = selected
        self.ignored_switches: set[str] = set()
        self.rejected_switches: set[str] = set()
        self.expired = False
        self.unavailable: Exception | None = None
        self.switches: list[str] = []
        self.reads: list[tuple[str, str]] = []
        httpx_mock.add_callback(
            self._start_page, url=HUB + "/", is_optional=True, is_reusable=True,
        )
        httpx_mock.add_callback(
            self._switch, url=re.compile(re.escape(HUB) + r"/Account/PupilSwitcher/SwitchPupil/.+"),
            is_optional=True, is_reusable=True,
        )

    def page(self) -> str:
        routes = ", ".join(f"'/Account/PupilSwitcher/SwitchPupil/{id_}'" for id_ in self.children)
        name = self.children[self.selected]
        return (
            f"<script>IMHome.init = {{ selectedPupilName: '{name}' }};\n"
            f"var pupils = [{routes}];</script>"
        )

    def route(self, path, handler, *, method="POST"):
        """Answer `path` (any query) with handler(request, pupil): JSON data or a Response."""

        def respond(request):
            self.reads.append((path, self.selected))
            result = handler(request, self.selected)
            if isinstance(result, httpx.Response):
                return result
            return httpx.Response(200, json=result)

        self.httpx_mock.add_callback(
            respond, url=re.compile(re.escape(HUB + path) + r"(\?.*)?$"), method=method,
            is_optional=True, is_reusable=True,
        )

    def read_by(self, path) -> list[str]:
        """The pupil selected at each read of `path`."""
        return [pupil for read_path, pupil in self.reads if read_path == path]

    def requests(self, path) -> list[httpx.Request]:
        return [r for r in self.httpx_mock.get_requests() if r.url.path == path]

    def _start_page(self, request):
        if self.unavailable is not None:
            raise self.unavailable
        if self.expired:
            return httpx.Response(302, headers={"location": "/Authentication/Authentication/Login"})
        return httpx.Response(200, text=self.page())

    def _switch(self, request):
        child_id = request.url.path.rsplit("/", 1)[1]
        self.switches.append(child_id)
        if child_id in self.rejected_switches:
            return httpx.Response(401)
        if child_id in self.children and child_id not in self.ignored_switches:
            self.selected = child_id
        return httpx.Response(200, text=self.page())
