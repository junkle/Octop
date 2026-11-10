import { describe, expect, it } from "vitest";
import { buildBackendSpec, preserveBackendRuntime } from "./agentBackendForm";

describe("expert backend runtime fields", () => {
  it("preserves shell environment isolation when editing root_dir", () => {
    const next = buildBackendSpec("local_shell", "local_shell", [], "/new");
    expect(
      preserveBackendRuntime(
        {
          type: "local_shell",
          root_dir: "/old",
          inherit_env: false,
          env: { PYTHONPATH: "/.runtime" },
          unused: "discard",
        },
        next,
      ),
    ).toEqual({
      type: "local_shell",
      root_dir: "/new",
      virtual_mode: true,
      inherit_env: false,
      env: { PYTHONPATH: "/.runtime" },
    });
  });

  it("does not carry local runtime fields into another backend type", () => {
    const next = buildBackendSpec("filesystem", "local_shell", [], "/new");
    expect(
      preserveBackendRuntime(
        { type: "local_shell", inherit_env: false, env: { HOME: "/" } },
        next,
      ),
    ).toEqual(next);
  });

  it("preserves runtime fields nested in a composite backend", () => {
    expect(
      preserveBackendRuntime(
        {
          type: "composite",
          default: {
            type: "local_shell",
            inherit_env: false,
            env: { HOME: "/" },
          },
          routes: {
            "/data": {
              type: "filesystem",
              inherit_env: false,
              env: { LANG: "C.UTF-8" },
            },
          },
        },
        {
          type: "composite",
          default: { type: "local_shell", root_dir: "/new" },
          routes: {
            "/data": { type: "filesystem", root_dir: "/new-data" },
          },
        },
      ),
    ).toEqual({
      type: "composite",
      default: {
        type: "local_shell",
        root_dir: "/new",
        inherit_env: false,
        env: { HOME: "/" },
      },
      routes: {
        "/data": {
          type: "filesystem",
          root_dir: "/new-data",
          inherit_env: false,
          env: { LANG: "C.UTF-8" },
        },
      },
    });
  });
});
