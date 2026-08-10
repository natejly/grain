// Entry for the React runtime that ships inside project sandbox frames.
//
// The frame has no network, so React cannot be fetched there. This bundle is
// inlined into the iframe ahead of the user's code and hands the modules to the
// bundler's bare-specifier shims through one global. Namespace objects are used
// rather than re-exports so the shims can hand back both named members and a
// usable default.
import * as React from "react";
import * as ReactDOM from "react-dom";
import * as ReactDOMClient from "react-dom/client";
import * as JsxRuntime from "react/jsx-runtime";

globalThis.__PROJECT_REACT__ = {
  react: React,
  "react-dom": ReactDOM,
  "react-dom/client": ReactDOMClient,
  "react/jsx-runtime": JsxRuntime,
  // jsxDEV takes extra debug arguments that jsx ignores, so aliasing is safe
  // and saves shipping React's development JSX runtime as well.
  "react/jsx-dev-runtime": { ...JsxRuntime, jsxDEV: JsxRuntime.jsx },
};
