"""The deploy workflow must not let an unpublished commit look published.

`CLOUDFLARE_ACCOUNT_ID` is set on this repository and `CLOUDFLARE_API_TOKEN` is
not, and that is the state the site spent four commits in: every `Site` run
reported **success** while `meow-bot-site.pages.dev` served commit `0af327e`,
from before all four of them. The build guards were not wrong — the build was
fine. What reached a person was one line, `success`, which was true of the build
and false of the deploy, and a `::notice` explaining the difference that does not
appear on the checks list.

So these tests are about the two ways that can come back:

1. the missing-token path going quiet again, and
2. a step that reports success for "Cloudflare accepted the upload", which is a
   different claim from "the site serves this commit".

The workflow is read as text rather than parsed: `PyYAML` is not a dependency of
this project and adding one to assert four lines is a poor trade.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "site.yml"


class AnUnpublishedCommitIsNotGreen(unittest.TestCase):
    def setUp(self):
        self.text = WORKFLOW.read_text(encoding="utf-8")
        # Everything from the configuration check to the end of the file, i.e.
        # the steps that decide whether anything is published.
        self.deploy = self.text.split("Is Cloudflare configured?", 1)[1]

    def test_the_missing_token_path_warns_and_says_so_on_the_run(self):
        """A notice is invisible on the checks list; a warning is not."""
        self.assertIn("::warning title=This commit was NOT published", self.deploy)
        self.assertNotIn("::notice::CLOUDFLARE_API_TOKEN is not set", self.deploy,
                         "back to a notice, which no checks list shows")

    def test_the_missing_token_path_writes_a_job_summary_naming_the_fix(self):
        """The run page is where somebody looks after a green build and a stale
        site, so the answer has to be there — not only in a log nobody opened."""
        self.assertIn("GITHUB_STEP_SUMMARY", self.deploy)
        self.assertIn("Published: **no**", self.deploy)
        self.assertIn("gh secret set CLOUDFLARE_API_TOKEN", self.deploy)
        self.assertIn("Cloudflare Pages: Edit", self.deploy)

    def test_the_summary_does_not_report_success_while_skipping_the_publish(self):
        self.assertNotIn("Published: **yes**", self.deploy)

    def test_a_rejected_token_fails_before_wrangler_starts(self):
        """An expired token otherwise reads as a deploy failure with no reason.

        The refusal says *cannot publish* rather than *was rejected*, because
        valid-but-scoped-wrongly is the case that actually happened here.
        """
        self.assertIn("/user/tokens/verify", self.deploy)
        self.assertIn("::error title=The Cloudflare token cannot publish", self.deploy)

    def test_the_preflight_asks_whether_the_token_can_publish(self):
        """`Pages Read` reads every GET and cannot deploy.

        Measured on the first real run of this workflow, against this very
        account: the deployed token answers `200` to the project read and `403`
        to the upload-token call, and the publish died inside wrangler as
        `Authentication error [code: 10000]`. A preflight that only *read* would
        have waved it through and let the site look broken.

        So the check is the capability the deploy actually needs — a Pages upload
        token, which creates nothing and publishes nothing — and the refusal names
        the permission rather than quoting a stack trace.
        """
        self.assertIn("/upload-token", self.deploy)
        self.assertIn("Cloudflare Pages: Edit", self.deploy)
        self.assertIn("Pages Write", self.deploy)
        self.assertIn("cannot publish", self.deploy)
        # The refusal has to depend on the publish check, not on the project read:
        # a read-only token passes the read and must still be refused.
        self.assertNotRegex(
            self.deploy,
            r"if \[ \"\$read_status\" != \"200\" \]",
            "acceptance is decided by the read, which a read-only token passes",
        )

    def test_a_missing_account_id_is_refused_before_anything_is_asked(self):
        """It used to pass when the token endpoint said 200, and then fail in
        wrangler with the project unnamed — the least actionable version."""
        before_any_call = self.deploy.split("token_status=", 1)[0]
        self.assertIn("CLOUDFLARE_ACCOUNT_ID is not", before_any_call)
        self.assertIn("exit 1", before_any_call)

    def test_an_account_owned_token_is_not_refused_by_the_token_endpoint(self):
        """The token endpoint answers 401 for a token that works.

        Measured against the real API rather than read off the docs: the deployed
        `CLOUDFLARE_API_TOKEN` returns `401 Invalid API Token` from
        `/user/tokens/verify` and `200` from the account's Pages project, because
        Cloudflare rejects account-owned tokens at the token endpoint by design —
        and a Pages-scoped token is usually account-owned.

        So the first version of this preflight was itself the defect it was added
        to prevent: it would have red-failed a perfectly good token *before*
        wrangler ran, turning `PLAN_CHECKOUT`-style silence into a blocked deploy.
        The project endpoint is asked as well, and only both failing is a refusal.
        """
        self.assertIn("accounts/${ACCOUNT_ID}/pages/projects/", self.deploy)
        self.assertIn("account-owned", self.deploy)
        self.assertIn("CLOUDFLARE_ACCOUNT_ID is not", self.deploy)
        # Order, not just presence: the refusal has to come *after* the Pages
        # project has been asked, or a 401 from the token endpoint is fatal on its
        # own and an account-owned token cannot publish even when it should.
        self.assertLess(
            self.deploy.index("pages/projects/"),
            self.deploy.index("::error title=The Cloudflare token cannot publish"),
            "the token is refused before the Pages project is consulted",
        )

    def test_the_publish_is_skipped_only_when_the_token_was_accepted(self):
        self.assertIn("if: steps.cf.outputs.configured == 'true'", self.deploy)

    def test_an_accepted_upload_is_not_taken_as_a_published_site(self):
        """`wrangler pages deploy` exiting 0 means the upload was accepted.

        The site sat four commits behind a green workflow because that was the
        only claim anybody made. The last step asks Cloudflare which commit
        production serves and fails when it is not this one.
        """
        self.assertIn("Check the deployment actually landed on this commit", self.text)
        self.assertIn("EXPECTED: ${{ github.sha }}", self.deploy)
        self.assertIn("::error title=The site did not take this commit", self.deploy)
        self.assertRegex(self.deploy.split("Check the deployment actually landed", 1)[1],
                         r"exit 1")

    def test_the_deployment_is_attributable_to_a_commit(self):
        """The deploy job downloads an artifact and never checks out the repo, so
        wrangler finds no git repository and attaches no commit unless it is told
        one — and then 'did this commit reach the site?' has no answer."""
        self.assertRegex(self.deploy, r"--commit-hash=\$\{\{ github\.sha \}\}")

    def test_no_untrusted_commit_message_reaches_a_shell(self):
        """`github.event.head_commit.message` is attacker-influenced text, and it
        would be interpolated into a step that holds a deploy token."""
        self.assertNotIn("head_commit.message", self.text)

    def test_every_mention_of_the_pages_project_is_the_same_project(self):
        """A rename has to reach four places, and this workflow has four.

        The project was renamed `meow-bot-site` -> `meow-bot`, and the last line
        kept announcing the old name: `meow-bot-site.pages.dev` now answers **530
        — project not found**, so the success line of a *successful* deploy
        pointed at a host that does not exist, and the one place a person would
        click to check the result was the one place that was wrong. Nothing else
        broke, because the API calls and the `--project-name` were all updated in
        the same commit that renamed it — which is exactly why the leftover
        mattered: it is only ever read by whoever is deciding whether the deploy
        worked.

        So the four are compared to each other rather than each written down, in
        both directions: every `pages/projects/<name>` the workflow asks, the
        project it names to wrangler, and the URL it prints must be one name, or
        this fails.
        """
        asked = re.search(r"--project-name=([A-Za-z0-9-]+)", self.deploy)
        self.assertIsNotNone(asked, "the publish step names no --project-name")
        project = asked.group(1)

        self.assertEqual(
            set(re.findall(r"pages/projects/([A-Za-z0-9-]+)", self.deploy)),
            {project},
            "the workflow talks to a different Pages project than it deploys to",
        )

        landed = self.deploy.split("Check the deployment actually landed", 1)[1]
        announced = re.search(r"https://([A-Za-z0-9-]+)\.pages\.dev", landed)
        self.assertIsNotNone(announced, "the landed check announces no site URL")
        self.assertEqual(
            announced.group(1),
            project,
            "the URL announced after a successful publish is not the project it published",
        )


if __name__ == "__main__":
    unittest.main()
