/** A path under `/owner` that is not one of the console's own addresses.
 *
 * The router used to answer every unmatched path with the **Overview** page, and
 * driving it is how that showed up: `/owner/nonsense` rendered a screen that
 * looked right, at an address that was not. Nothing was visibly broken, which is
 * exactly the problem — the rail's `aria-current` correctly highlighted nothing,
 * so the only evidence that this was not a page was a missing highlight, and
 * somebody following a retired bookmark or mistyping a server page had no way to
 * find that out. It also made the console disagree with itself: an unknown
 * *server* already answered "No such server", while an unknown *page* answered
 * with a page.
 *
 * The shell is still drawn around this, so the rail is the way out and the button
 * repeats it for anyone who does not think to look left.
 *
 * The sentence under the heading used to end *"There are five pages, all in the
 * rail"*, and the rail has ten: a count written beside a list is the one claim in
 * it a reader cannot check and the one that goes stale in silence, so it is gone
 * rather than updated. `tests/test_console.py` holds the sentence to that.
 */

import { Link } from "../lib/router";
import { Card, Empty } from "../ui";

export function Missing({ path }: { path: string }) {
  return (
    <div className="grid">
      <div className="page__head">
        <div>
          <span className="eyebrow">Not a page here</span>
          <h1>Nothing at this address</h1>
        </div>
      </div>
      <Card span={6} index={0}>
        <Empty
          title={`/owner${path} is not one of this console's pages`}
          body="A mistyped address, or a link to something that has been renamed. Every page this console has is in the rail."
          action={
            <Link className="btn btn--primary" to="/">
              Open the Overview
            </Link>
          }
        />
      </Card>
    </div>
  );
}
