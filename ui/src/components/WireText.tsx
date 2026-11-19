/** Text that came off the bus. Always a React text node (never HTML); full string in the tooltip,
 *  truncation is CSS only (DESIGN.md "Numbers"). eslint bans dangerouslySetInnerHTML. */
export function WireText({ value }: { value: string }) {
  return (
    <span className="wire-text" title={value}>
      {value}
    </span>
  );
}
