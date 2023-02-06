#!/bin/bash

if [ -n "$SITE_NAME" ]; then
  waldur constance set SITE_NAME "$SITE_NAME"
  echo "[+] SITE_NAME has been set"
fi

if [ -n "$SITE_LOGO" ]; then
  waldur constance set SITE_LOGO "$SITE_LOGO"
  echo "[+] SITE_LOGO has been set"
fi

if [ -n "$SITE_ADDRESS" ]; then
  waldur constance set SITE_ADDRESS "$SITE_ADDRESS"
  echo "[+] SITE_ADDRESS has been set"
fi

if [ -n "$SITE_EMAIL" ]; then
  waldur constance set SITE_EMAIL "$SITE_EMAIL"
  echo "[+] SITE_EMAIL has been set"
fi

if [ -n "$SITE_PHONE" ]; then
  waldur constance set SITE_PHONE "$SITE_PHONE"
  echo "[+] SITE_PHONE has been set"
fi

if [ -n "$SHORT_PAGE_TITLE" ]; then
  waldur constance set SHORT_PAGE_TITLE "$SHORT_PAGE_TITLE"
  echo "[+] SHORT_PAGE_TITLE has been set"
fi

if [ -n "$FULL_PAGE_TITLE" ]; then
  waldur constance set FULL_PAGE_TITLE "$FULL_PAGE_TITLE"
  echo "[+] FULL_PAGE_TITLE has been set"
fi

if [ -n "$BRAND_COLOR" ]; then
  waldur constance set BRAND_COLOR "$BRAND_COLOR"
  echo "[+] BRAND_COLOR has been set"
fi

if [ -n "$HERO_LINK_LABEL" ]; then
  waldur constance set HERO_LINK_LABEL "$HERO_LINK_LABEL"
  echo "[+] HERO_LINK_LABEL has been set"
fi

if [ -n "$HERO_LINK_URL" ]; then
  waldur constance set HERO_LINK_URL "$HERO_LINK_URL"
  echo "[+] HERO_LINK_URL has been set"
fi

if [ -n "$SITE_DESCRIPTION" ]; then
  waldur constance set SITE_DESCRIPTION "$SITE_DESCRIPTION"
  echo "[+] SITE_DESCRIPTION has been set"
fi

if [ -n "$CURRENCY_NAME" ]; then
  waldur constance set CURRENCY_NAME "$CURRENCY_NAME"
  echo "[+] CURRENCY_NAME has been set"
fi

if [ -n "$DOCS_URL" ]; then
  waldur constance set DOCS_URL "$DOCS_URL"
  echo "[+] DOCS_URL has been set"
fi

echo "[+] Whitelabeling settings have been set"
