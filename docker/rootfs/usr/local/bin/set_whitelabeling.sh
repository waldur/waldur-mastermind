#!/bin/bash

if [ -n "$SITE_NAME" ]; then
  waldur constance set SITE_NAME "$SITE_NAME"
  echo "[+] SITE_NAME has been set to: $SITE_NAME"
fi

if [ -n "$SITE_ADDRESS" ]; then
  waldur constance set SITE_ADDRESS "$SITE_ADDRESS"
  echo "[+] SITE_ADDRESS has been set to: $SITE_ADDRESS"
fi

if [ -n "$SITE_EMAIL" ]; then
  waldur constance set SITE_EMAIL "$SITE_EMAIL"
  echo "[+] SITE_EMAIL has been set to: $SITE_EMAIL"
fi

if [ -n "$SITE_PHONE" ]; then
  waldur constance set SITE_PHONE "$SITE_PHONE"
  echo "[+] SITE_PHONE has been set to: $SITE_PHONE"
fi

if [ -n "$SHORT_PAGE_TITLE" ]; then
  waldur constance set SHORT_PAGE_TITLE "$SHORT_PAGE_TITLE"
  echo "[+] SHORT_PAGE_TITLE has been set to: $SHORT_PAGE_TITLE"
fi

if [ -n "$FULL_PAGE_TITLE" ]; then
  waldur constance set FULL_PAGE_TITLE "$FULL_PAGE_TITLE"
  echo "[+] FULL_PAGE_TITLE has been set to: $FULL_PAGE_TITLE"
fi

if [ -n "$BRAND_COLOR" ]; then
  waldur constance set BRAND_COLOR "$BRAND_COLOR"
  echo "[+] BRAND_COLOR has been set to: $BRAND_COLOR"
fi

if [ -n "$HERO_LINK_LABEL" ]; then
  waldur constance set HERO_LINK_LABEL "$HERO_LINK_LABEL"
  echo "[+] HERO_LINK_LABEL has been set to: $HERO_LINK_LABEL"
fi

if [ -n "$HERO_LINK_URL" ]; then
  waldur constance set HERO_LINK_URL "$HERO_LINK_URL"
  echo "[+] HERO_LINK_URL has been set to: $HERO_LINK_URL"
fi

if [ -n "$SITE_DESCRIPTION" ]; then
  waldur constance set SITE_DESCRIPTION "$SITE_DESCRIPTION"
  echo "[+] SITE_DESCRIPTION has been set to: $SITE_DESCRIPTION"
fi

if [ -n "$CURRENCY_NAME" ]; then
  waldur constance set CURRENCY_NAME "$CURRENCY_NAME"
  echo "[+] CURRENCY_NAME has been set to: $CURRENCY_NAME"
fi

if [ -n "$DOCS_URL" ]; then
  waldur constance set DOCS_URL "$DOCS_URL"
  echo "[+] DOCS_URL has been set to: $DOCS_URL"
fi

if [ -n "$SUPPORT_PORTAL_URL" ]; then
  waldur constance set SUPPORT_PORTAL_URL "$SUPPORT_PORTAL_URL"
  echo "[+] SUPPORT_PORTAL_URL has been set to: $SUPPORT_PORTAL_URL"
fi

if [ -n "$POWERED_BY_LOGO" ]; then
  if [ -f "$POWERED_BY_LOGO" ]; then
    waldur set_constance_image POWERED_BY_LOGO "$POWERED_BY_LOGO"
    echo "[+] POWERED_BY_LOGO has been set"
  else
    echo "[-] ERROR: $POWERED_BY_LOGO file does not exist"
  fi
fi

if [ -n "$HERO_IMAGE" ]; then
  if [ -f "$HERO_IMAGE" ]; then
    waldur set_constance_image HERO_IMAGE "$HERO_IMAGE"
    echo "[+] HERO_IMAGE has been set"
  else
    echo "[-] ERROR: $HERO_IMAGE file does not exist"
  fi
fi

if [ -n "$SIDEBAR_LOGO" ]; then
  if [ -f "$SIDEBAR_LOGO" ]; then
    waldur set_constance_image SIDEBAR_LOGO "$SIDEBAR_LOGO"
    echo "[+] SIDEBAR_LOGO has been set"
  else
    echo "[-] ERROR: $SIDEBAR_LOGO file does not exist"
  fi
fi

if [ -n "$SIDEBAR_LOGO_MOBILE" ]; then
  if [ -f "$SIDEBAR_LOGO_MOBILE" ]; then
    waldur set_constance_image SIDEBAR_LOGO_MOBILE "$SIDEBAR_LOGO_MOBILE"
    echo "[+] SIDEBAR_LOGO_MOBILE has been set"
  else
    echo "[-] ERROR: $SIDEBAR_LOGO_MOBILE file does not exist"
  fi
fi

if [ -n "$SITE_LOGO" ]; then
  if [ -f "$SITE_LOGO" ]; then
    waldur set_constance_image SITE_LOGO "$SITE_LOGO"
    echo "[+] SITE_LOGO has been set"
  else
    echo "[-] ERROR: $SITE_LOGO file does not exist"
  fi
fi

if [ -n "$LOGIN_LOGO" ]; then
  if [ -f "$LOGIN_LOGO" ]; then
    waldur set_constance_image LOGIN_LOGO "$LOGIN_LOGO"
    echo "[+] LOGIN_LOGO has been set"
  else
    echo "[-] ERROR: $LOGIN_LOGO file does not exist"
  fi
fi

if [ -n "$FAVICON" ]; then
  if [ -f "$FAVICON" ]; then
    waldur set_constance_image FAVICON "$FAVICON"
    echo "[+] FAVICON has been set"
  else
    echo "[-] ERROR: $FAVICON file does not exist"
  fi
fi

echo "[+] Whitelabeling settings have been set"
