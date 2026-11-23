/**
 * Algorithms: each business element, and the generation rule that makes it.
 *
 * One framework makes every value (sdg/engine/rules.py): a business element's
 * rule names an algorithm and its settings, and files, EDI and database loads
 * all run it. This is where those are read and changed, outside the workflow.
 */

import { Rules } from "./Rules";

export function Algorithms() {
  return (
    <div className="algorithms">
      <p className="panel__hint">
        Each business element and the rule that generates its values in every output.
      </p>
      <Rules />
    </div>
  );
}
