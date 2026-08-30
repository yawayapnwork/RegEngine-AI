export default function Card({ children, className = "", ...props }) {
  return (
    <div
      className={`rounded-sm border border-ink-700 bg-ink-900 ${className}`}
      {...props}
    >
      {children}
    </div>
  );
}
