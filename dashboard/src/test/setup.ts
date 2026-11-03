import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

// React Flow (the Diagram tab) needs these, which jsdom doesn't have. Nothing is measured in
// jsdom: boxes carry their own width and height, so they render anyway.
if (typeof globalThis.ResizeObserver === "undefined") {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
}
if (typeof globalThis.DOMMatrixReadOnly === "undefined") {
  // React Flow reads only the scale (`m22`) of a transform.
  globalThis.DOMMatrixReadOnly = class {
    m22: number;
    constructor(transform?: string) {
      const scale = /scale\(([\d.]+)\)/.exec(transform ?? "")?.[1];
      this.m22 = scale ? Number(scale) : 1;
    }
  } as unknown as typeof DOMMatrixReadOnly;
}

afterEach(() => {
  cleanup();
  window.location.hash = "";
  window.localStorage.clear();
});
