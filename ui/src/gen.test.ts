import { expect, test } from "vitest";
import { Meta } from "./gen/common";
import { TargetPosition } from "./gen/live";

test("generated protobuf types encode/decode", () => {
  const meta = Meta.fromPartial({ msgId: "abc", producer: "exo", sentNs: 123n, schemaVersion: 1 });
  const bytes = Meta.encode(meta).finish();
  expect(Meta.decode(bytes)).toEqual(meta);

  const target = TargetPosition.fromPartial({ bookId: 7, targetQtyE2: 100n });
  expect(target.bookId).toBe(7);
});
