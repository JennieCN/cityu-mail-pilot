import Link from "next/link";
import type { ComponentPropsWithoutRef, CSSProperties, ReactNode } from "react";
import { ArrowIcon } from "./icons";

export type CSSVars = CSSProperties & {
  "--i"?: number | string;
};

export function MeshBackground() {
  return (
    <div className="mesh" aria-hidden="true">
      <i />
      <i />
      <i />
      <i />
    </div>
  );
}

export function GlassLayer() {
  return (
    <span className="lg" aria-hidden="true">
      <i className="lg-body" />
      <i className="lg-spec" />
    </span>
  );
}

type ButtonLinkProps = ComponentPropsWithoutRef<"a"> & {
  variant?: "primary" | "ghost" | "onDark";
  showIcon?: boolean;
};

export function ButtonLink({
  variant = "primary",
  showIcon = variant !== "ghost",
  children,
  className = "",
  ...props
}: ButtonLinkProps) {
  const variantClass =
    variant === "ghost" ? "pill-ghost" : variant === "onDark" ? "pill on-dark" : "pill";
  const linkClass = `${variantClass} ${className}`.trim();

  const content = (
    <>
      {children}
      {showIcon ? (
        <span className="icon-wrap" aria-hidden="true">
          <ArrowIcon />
        </span>
      ) : null}
    </>
  );

  if (typeof props.href === "string" && props.href.startsWith("/")) {
    return (
      <Link className={linkClass} {...props} href={props.href}>
        {content}
      </Link>
    );
  }

  return (
    <a className={linkClass} {...props}>
      {content}
    </a>
  );
}

type ShellProps = {
  children: ReactNode;
  className?: string;
  coreClassName?: string;
  coreStyle?: CSSProperties;
  glass?: boolean;
};

export function Shell({
  children,
  className = "",
  coreClassName = "",
  coreStyle,
  glass = false,
}: ShellProps) {
  return (
    <div className={`shell ${className}`.trim()}>
      {glass ? <GlassLayer /> : null}
      <div className={`core ${coreClassName}`.trim()} style={coreStyle}>
        {children}
      </div>
    </div>
  );
}

export function Eyebrow({ children }: { children: ReactNode }) {
  return (
    <span className="eyebrow">
      <span className="dot" />
      {children}
    </span>
  );
}

export function SectionHead({
  title,
  children,
  xl = false,
}: {
  title: ReactNode;
  children: ReactNode;
  xl?: boolean;
}) {
  return (
    <div className="section-head">
      <div>
        <h2 className={`scrub ${xl ? "xl" : ""}`.trim()}>{title}</h2>
      </div>
      <p className="rv" style={{ "--i": 1 } as CSSVars}>
        {children}
      </p>
    </div>
  );
}

export function FeatureCard({
  meta,
  title,
  children,
  className = "span-3",
  dark = false,
}: {
  meta: string;
  title: string;
  children: ReactNode;
  className?: string;
  dark?: boolean;
}) {
  return (
    <article className={`shell lift b-card ${className} ${dark ? "dark" : ""}`.trim()}>
      <div className="core">
        <div className="meta">{meta}</div>
        <h3>{title}</h3>
        {children}
      </div>
    </article>
  );
}
