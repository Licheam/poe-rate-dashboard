import unittest

from server import extract_leaderboard_section, parse_leaderboard_items


SAMPLE_HTML = """
<html>
  <body>
    <section>
      <h2>Top Models</h2>
      <div class="leaderboard models">
        <div class="row"><span>#1</span><a href="/gpt-5">GPT-5</a><span>34.5%</span></div>
        <div class="row"><span>#2</span><a href="/claude-sonnet-4.6">claude-sonnet-4.6</a><span>21%</span></div>
        <div class="row"><span>#3</span><a href="/gemini-3.1-pro">gemini-3.1-pro</a><span>10.25%</span></div>
      </div>
    </section>
    <section>
      <h2>Top Apps</h2>
      <div class="leaderboard apps">
        <div class="row"><span>#1</span><a href="/app-one">App-One</a><span>40%</span></div>
        <div class="row"><span>#2</span><a href="/app-two">App-Two</a><span>20%</span></div>
        <div class="row"><span>#3</span><a href="/app-three">App-Three</a><span>15%</span></div>
      </div>
    </section>
  </body>
</html>
"""


class LeaderboardParsingTests(unittest.TestCase):
    def test_extract_models_section(self):
        section = extract_leaderboard_section(SAMPLE_HTML, "models")
        items = parse_leaderboard_items(section, 2)

        self.assertEqual(
            items,
            [
                {"rank": 1, "handle": "GPT-5", "market_share": "34.5%"},
                {"rank": 2, "handle": "Claude-sonnet-4.6", "market_share": "21%"},
            ],
        )

    def test_extract_apps_section(self):
        section = extract_leaderboard_section(SAMPLE_HTML, "apps")
        items = parse_leaderboard_items(section, 3)

        self.assertEqual(items[0], {"rank": 1, "handle": "App-One", "market_share": "40%"})
        self.assertEqual(len(items), 3)


if __name__ == "__main__":
    unittest.main()
