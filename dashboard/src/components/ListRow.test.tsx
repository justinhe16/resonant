import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ListRow } from "@/components/ListRow";

const NOW = Date.parse("2026-10-16T09:00:00Z");

function row(props: Partial<Parameters<typeof ListRow>[0]> = {}) {
  render(
    <ul>
      <ListRow title="Fix the shell gate so simple calculator math works again" now={NOW} {...props} />
    </ul>,
  );
  return screen.getByTestId(props.testId ?? "list-row");
}

describe("ListRow", () => {
  it("is ref · one title line · pill · relative time, and opens its dialog", () => {
    const li = row({
      to: { kind: "run", code: "R11" },
      refText: "R11",
      pill: { tone: "ok", label: "merged", title: "by resonant" },
      time: "2026-10-16T07:00:00Z",
    });
    expect(li.tagName).toBe("LI");
    const parts = Array.from(li.children).map((c) => c.textContent);
    expect(parts).toEqual(["R11", "Fix the shell gate so simple calculator math works again", "merged", "2h"]);
    const link = within(li).getByRole("link");
    expect(link).toHaveAttribute("href", "#/runs/R11");
    expect(link).toHaveAccessibleName("Fix the shell gate so simple calculator math works again");
    expect(link).toHaveClass("truncate"); // one line, cut with an ellipsis
    expect(link.className).toMatch(/after:absolute/); // the whole row is the target
    expect(within(li).getByText("merged").closest("[title]")).toHaveAttribute("title", "by resonant");
    const time = within(li).getByText("2h");
    expect(time.tagName).toBe("TIME");
    expect(time).toHaveAttribute("dateTime", "2026-10-16T07:00:00Z");
  });

  it("shows at most one hint, hidden on a phone", () => {
    const li = row({ hint: "resonant" });
    const hint = within(li).getByTestId("row-hint");
    expect(hint).toHaveTextContent("resonant");
    expect(hint).toHaveClass("hidden", "sm:inline");
  });

  it("badges outside text outside the cut title, and renders it as text", () => {
    const li = row({ title: "<b>do this</b>", outside: true, to: { kind: "task", id: "t1" } });
    expect(within(li).getByRole("button", { name: /Outside text/ })).toBeInTheDocument();
    expect(within(li).getByRole("link")).toHaveTextContent("<b>do this</b>");
    expect(li.querySelector("b")).toBeNull();
  });

  it("is plain text without a route, and leaves out what it isn't given", () => {
    const li = row({ title: "Backup finished" });
    expect(within(li).queryByRole("link")).not.toBeInTheDocument();
    expect(li.querySelector("time")).toBeNull();
    expect(within(li).queryByTestId("row-hint")).not.toBeInTheDocument();
    expect(li).toHaveTextContent(/^Backup finished$/);
  });

  it("puts the full id in the ref's tooltip", () => {
    const li = row({ refText: "4f1a9c", refTitle: "t_4f1a9c" });
    expect(within(li).getByText("4f1a9c")).toHaveAttribute("title", "t_4f1a9c");
  });
});
