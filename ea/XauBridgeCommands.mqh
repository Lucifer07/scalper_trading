//+------------------------------------------------------------------+
//| XauBridgeCommands.mqh — perintah bridge (dipakai XauBridge.mq4)  |
//| Protokol: <id>|<cmd>|k=v;k=v  ->  <id>|ok=1|k=v;...              |
//+------------------------------------------------------------------+

string PStr(const string ps, const string key, const string dflt = "")
{
   string seg[];
   int n = StringSplit(ps, ';', seg);
   for(int i = 0; i < n; i++)
   {
      int eq = StringFind(seg[i], "=");
      if(eq <= 0) continue;
      if(StringSubstr(seg[i], 0, eq) == key) return StringSubstr(seg[i], eq + 1);
   }
   return dflt;
}

double PNum(const string ps, const string key, const double dflt = 0.0)
{
   string v = PStr(ps, key, "");
   return (v == "") ? dflt : StrToDouble(v);
}

string Sane(const string v)
{
   string o = v;
   StringReplace(o, ";", "_");
   StringReplace(o, "|", "_");
   StringReplace(o, "=", "_");
   StringReplace(o, "\n", "_");
   StringReplace(o, "\r", "_");
   return o;
}

string Dbl(const double v) { return DoubleToString(v, 8); }
string B(const bool v)     { return v ? "1" : "0"; }

void SendLine(const string line)
{
   if(g_sock < 0) return;
   uchar buf[];
   int len = StringToCharArray(line + "\n", buf, 0, -1, CP_UTF8);
   if(len <= 1) return;
   if(send(g_sock, buf, len - 1, 0) < 0)
      Drop("send err=" + IntegerToString(WSAGetLastError()));
}

void Reply(const string id, const bool ok, const string body)
{
   string line = id + "|ok=" + B(ok);
   if(StringLen(body) > 0) line += "|" + body;
   SendLine(line);
}


int TFFromName(const string s)
{
   if(s == "M1")  return PERIOD_M1;
   if(s == "M5")  return PERIOD_M5;
   if(s == "M15") return PERIOD_M15;
   if(s == "M30") return PERIOD_M30;
   if(s == "H1")  return PERIOD_H1;
   if(s == "H4")  return PERIOD_H4;
   if(s == "D1")  return PERIOD_D1;
   if(s == "W1")  return PERIOD_W1;
   return -1;
}

// Bar tertutup saja (index 0 = bar berjalan, dibuang). Waktu = server time;
// offset GMT dikirim sekali agar Python bisa konversi ke UTC.
void CmdRates(const string id, const string ps)
{
   string sym = PStr(ps, "sym");
   string tfname = PStr(ps, "tf", "H1");
   int count = (int)PNum(ps, "count", 200);
   int forming = (int)PNum(ps, "forming", 0);
   int tf = TFFromName(tfname);
   if(tf < 0) { Reply(id, false, "err=bad_tf;msg=" + Sane(tfname)); return; }
   if(count < 2 || count > 500)
   { Reply(id, false, "err=bad_count;msg=" + IntegerToString(count)); return; }
   int total = iBars(sym, tf);
   if(total <= 0) { Reply(id, false, "err=no_history;msg=" + Sane(sym)); return; }
   int last = count;
   if(last > total) last = total;
   string out = "";
   int n = 0;
   int lowest = (forming != 0) ? 0 : 1;   // forming=1 ikutkan bar berjalan
   for(int i = last - 1; i >= lowest; i--)
   {
      datetime t = iTime(sym, tf, i);
      if(t <= 0) continue;
      if(n > 0) out += "~";
      out += IntegerToString((int)t) + "," + Dbl(iOpen(sym, tf, i)) + ","
           + Dbl(iHigh(sym, tf, i)) + "," + Dbl(iLow(sym, tf, i)) + ","
           + Dbl(iClose(sym, tf, i)) + "," + IntegerToString((int)iVolume(sym, tf, i));
      n++;
   }
   Reply(id, true, "sym=" + Sane(sym) + ";tf=" + tfname + ";count=" + IntegerToString(n)
                 + ";gmt=" + IntegerToString((int)(TimeCurrent() - TimeGMT()))
                 + ";bars=" + out);
}

//---------------------------- helpers -----------------------------

void CmdPing(const string id)
{
   Reply(id, true,
      "build=1"
      + ";login=" + IntegerToString(AccountNumber())
      + ";server=" + Sane(AccountServer())
      + ";currency=" + Sane(AccountCurrency())
      + ";equity=" + Dbl(AccountEquity())
      + ";balance=" + Dbl(AccountBalance())
      + ";freemargin=" + Dbl(AccountFreeMargin())
      + ";leverage=" + IntegerToString(AccountLeverage())
      + ";trading_allowed=" + B(g_allow)
      + ";terminal_trade=" + B(IsTradeAllowed())
      + ";connected=" + B(IsConnected())
      + ";server_time=" + IntegerToString((int)TimeCurrent())
      + ";frames=" + IntegerToString(g_frames));
}

void CmdSymbolInfo(const string id, const string ps)
{
   string sym = PStr(ps, "sym");
   double bid = MarketInfo(sym, MODE_BID);
   double ask = MarketInfo(sym, MODE_ASK);
   if(StringLen(sym) == 0 || bid <= 0 || ask <= 0)
   {
      Reply(id, false, "err=unknown_symbol;msg=" + Sane(sym));
      return;
   }
   Reply(id, true,
      "sym=" + Sane(sym)
      + ";digits=" + IntegerToString((int)MarketInfo(sym, MODE_DIGITS))
      + ";point=" + Dbl(MarketInfo(sym, MODE_POINT))
      + ";ticksize=" + Dbl(MarketInfo(sym, MODE_TICKSIZE))
      + ";tickvalue=" + Dbl(MarketInfo(sym, MODE_TICKVALUE))
      + ";stoplevel=" + IntegerToString((int)MarketInfo(sym, MODE_STOPLEVEL))
      + ";freezelevel=" + IntegerToString((int)MarketInfo(sym, MODE_FREEZELEVEL))
      + ";lotmin=" + Dbl(MarketInfo(sym, MODE_MINLOT))
      + ";lotmax=" + Dbl(MarketInfo(sym, MODE_MAXLOT))
      + ";lotstep=" + Dbl(MarketInfo(sym, MODE_LOTSTEP))
      + ";marginreq=" + Dbl(MarketInfo(sym, MODE_MARGINREQUIRED))
      + ";spread=" + IntegerToString((int)MarketInfo(sym, MODE_SPREAD))
      + ";bid=" + Dbl(bid)
      + ";ask=" + Dbl(ask)
      + ";tradeallowed=" + B(MarketInfo(sym, MODE_TRADEALLOWED) > 0));
}

void CmdPendingList(const string id, const string ps)
{
   string sym = PStr(ps, "sym");
   int magic = (int)PNum(ps, "magic", 0);
   string out = "";
   int count = 0;
   for(int i = 0; i < OrdersTotal(); i++)
   {
      if(!OrderSelect(i, SELECT_BY_POS, MODE_TRADES)) continue;
      if(sym != "" && OrderSymbol() != sym) continue;
      if(magic > 0 && OrderMagicNumber() != magic) continue;
      if(OrderType() == OP_BUY || OrderType() == OP_SELL) continue;  // posisi, bukan pending
      if(count > 0) out += "~";
      out += IntegerToString(OrderTicket());
      count++;
   }
   Reply(id, true, "count=" + IntegerToString(count) + ";tickets=" + out);
}

void CmdPositions(const string id, const string ps)
{
   string sym = PStr(ps, "sym");
   int magic = (int)PNum(ps, "magic", 0);
   string out = "";
   int count = 0;
   for(int i = 0; i < OrdersTotal(); i++)
   {
      if(!OrderSelect(i, SELECT_BY_POS, MODE_TRADES)) continue;
      if(sym != "" && OrderSymbol() != sym) continue;
      if(magic > 0 && OrderMagicNumber() != magic) continue;
      // Pending order (limit/stop) bukan posisi: jangan dihitung di sini.
      if(OrderType() != OP_BUY && OrderType() != OP_SELL) continue;
      string t = "other";
      if(OrderType() == OP_BUY)  t = "buy";
      if(OrderType() == OP_SELL) t = "sell";
      if(count > 0) out += "~";
      out += IntegerToString(OrderTicket()) + "," + t + "," + Dbl(OrderLots()) + ","
           + Dbl(OrderOpenPrice()) + "," + Dbl(OrderStopLoss()) + ","
           + Dbl(OrderTakeProfit()) + "," + Dbl(OrderProfit()) + ","
           + Sane(OrderComment());
      count++;
   }
   Reply(id, true, "count=" + IntegerToString(count) + ";pos=" + out);
}

// Pending limit: harga harus di sisi pasar yang benar dan tidak lebih dekat
// dari MODE_STOPLEVEL (kalau terlalu dekat, broker menolak).
string PendingInvalid(const int type, const double price, const double bid,
                      const double ask, const string sym)
{
   double stops = MarketInfo(sym, MODE_STOPLEVEL) * MarketInfo(sym, MODE_POINT);
   if(type == OP_BUYLIMIT)
   {
      if(price >= ask) return "buy_limit_above_market";
      if((ask - price) < stops) return "limit_too_close";
   }
   else if(type == OP_SELLLIMIT)
   {
      if(price <= bid) return "sell_limit_below_market";
      if((price - bid) < stops) return "limit_too_close";
   }
   else return "bad_pending_type";
   return "";
}

// Return kode error ("" = valid). Dipisah supaya CmdOrderSend tetap pendek.
string StopsInvalid(const int type, const double price, const double sl,
                    const double tp, const string sym)
{
   double stops = MarketInfo(sym, MODE_STOPLEVEL) * MarketInfo(sym, MODE_POINT);
   if(type == OP_BUY)
   {
      if(sl > 0 && sl >= price) return "sl_wrong_side";
      if(tp > 0 && tp <= price) return "tp_wrong_side";
      if(sl > 0 && (price - sl) < stops) return "sl_too_close";
      if(tp > 0 && (tp - price) < stops) return "tp_too_close";
   }
   else
   {
      if(sl > 0 && sl <= price) return "sl_wrong_side";
      if(tp > 0 && tp >= price) return "tp_wrong_side";
      if(sl > 0 && (sl - price) < stops) return "sl_too_close";
      if(tp > 0 && (price - tp) < stops) return "tp_too_close";
   }
   return "";
}

void CmdOrderSend(const string id, const string ps)
{
   if(!g_allow) { Reply(id, false, "err=trading_disabled;msg=allow_trading off (ini/input)"); return; }
   if(!IsTradeAllowed()) { Reply(id, false, "err=terminal_blocked;msg=IsTradeAllowed=false"); return; }
   if(IsTradeContextBusy()) { Reply(id, false, "err=busy;msg=trade context busy"); return; }
   string sym = PStr(ps, "sym");
   string side = PStr(ps, "side");
   double lots = PNum(ps, "lots", 0);
   double sl = PNum(ps, "sl", 0);
   double tp = PNum(ps, "tp", 0);
   int magic = (int)PNum(ps, "magic", g_magic);
   int dev = (int)PNum(ps, "dev", InpMaxSlippagePoints);
   string comment = PStr(ps, "comment", "xau-monitor");
   double bid = MarketInfo(sym, MODE_BID);
   double ask = MarketInfo(sym, MODE_ASK);
   if(bid <= 0 || ask <= 0) { Reply(id, false, "err=unknown_symbol;msg=" + Sane(sym)); return; }
   if(side != "buy" && side != "sell") { Reply(id, false, "err=bad_side;msg=" + Sane(side)); return; }
   if(lots <= 0 || lots > InpMaxLot + 1e-8)
   {
      Reply(id, false, "err=lot_limit;msg=InpMaxLot=" + Dbl(InpMaxLot));
      return;
   }
   if((int)MarketInfo(sym, MODE_SPREAD) > InpMaxSpreadPoints)
   {
      Reply(id, false, "err=spread;msg=limit=" + IntegerToString(InpMaxSpreadPoints));
      return;
   }
   double minlot = MarketInfo(sym, MODE_MINLOT);
   double maxlot = MarketInfo(sym, MODE_MAXLOT);
   double step = MarketInfo(sym, MODE_LOTSTEP);
   if(lots < minlot || lots > maxlot)
   {
      Reply(id, false, "err=lot_bounds;msg=min=" + Dbl(minlot) + ";max=" + Dbl(maxlot));
      return;
   }
   if(step > 0 && MathAbs(MathMod(lots, step)) > 1e-8)
   {
      Reply(id, false, "err=lot_step;msg=" + Dbl(step));
      return;
   }
   string otype = PStr(ps, "otype", "market");
   int digits = (int)MarketInfo(sym, MODE_DIGITS);
   int type;
   double price;
   if(otype == "limit")
   {
      // Entry pending di level stop engine; SL/TP broker WAJIB kosong.
      double want = PNum(ps, "price", 0);
      if(want <= 0) { Reply(id, false, "err=bad_price;msg=limit tanpa price"); return; }
      type = (side == "buy") ? OP_BUYLIMIT : OP_SELLLIMIT;
      price = NormalizeDouble(want, digits);
      string pbad = PendingInvalid(type, price, bid, ask, sym);
      if(pbad != "") { Reply(id, false, "err=" + pbad + ";msg=price=" + Dbl(price)); return; }
      sl = 0; tp = 0;
   }
   else
   {
      type = (side == "buy") ? OP_BUY : OP_SELL;
      price = NormalizeDouble((type == OP_BUY) ? ask : bid, digits);
      if(sl > 0) sl = NormalizeDouble(sl, digits);
      if(tp > 0) tp = NormalizeDouble(tp, digits);
      string bad = StopsInvalid(type, price, sl, tp, sym);
      if(bad != "") { Reply(id, false, "err=" + bad + ";msg=price=" + Dbl(price)); return; }
   }
   int ticket = OrderSend(sym, type, lots, price, dev, sl, tp, Sane(comment),
                          magic, 0, clrNONE);
   if(ticket < 0)
   {
      int err = GetLastError();
      PrintFormat("OrderSend gagal err=%d lots=%s price=%s sl=%s tp=%s", err,
                  Dbl(lots), Dbl(price), Dbl(sl), Dbl(tp));
      Reply(id, false, "err=" + IntegerToString(err) + ";msg=ordersend_failed");
      return;
   }
   Reply(id, true, "ticket=" + IntegerToString(ticket) + ";price=" + Dbl(price)
                   + ";sl=" + Dbl(sl) + ";tp=" + Dbl(tp)
                   + ";otype=" + Sane(otype));
}

void CmdOrderDelete(const string id, const string ps)
{
   if(!g_allow) { Reply(id, false, "err=trading_disabled;msg=allow_trading off (ini/input)"); return; }
   int ticket = (int)PNum(ps, "ticket", 0);
   if(ticket <= 0) { Reply(id, false, "err=bad_ticket;msg=" + IntegerToString(ticket)); return; }
   if(!OrderSelect(ticket, SELECT_BY_TICKET, MODE_TRADES))
   {
      Reply(id, false, "err=ticket_not_found;msg=" + IntegerToString(GetLastError()));
      return;
   }
   if(OrderType() == OP_BUY || OrderType() == OP_SELL)
   {
      Reply(id, false, "err=is_position;msg=bukan pending order");
      return;
   }
   if(!OrderDelete(ticket))
   {
      Reply(id, false, "err=" + IntegerToString(GetLastError()) + ";msg=orderdelete_failed");
      return;
   }
   Reply(id, true, "ticket=" + IntegerToString(ticket));
}

void CmdOrderClose(const string id, const string ps)
{
   if(!g_allow) { Reply(id, false, "err=trading_disabled;msg=allow_trading off (ini/input)"); return; }
   int ticket = (int)PNum(ps, "ticket", 0);
   double lots = PNum(ps, "lots", 0);
   if(ticket <= 0) { Reply(id, false, "err=bad_ticket;msg=" + IntegerToString(ticket)); return; }
   if(!OrderSelect(ticket, SELECT_BY_TICKET, MODE_TRADES))
   {
      Reply(id, false, "err=ticket_not_found;msg=" + IntegerToString(GetLastError()));
      return;
   }
   if(OrderType() != OP_BUY && OrderType() != OP_SELL) { Reply(id, false, "err=not_a_position"); return; }
   if(lots <= 0 || lots > OrderLots() + 1e-8) lots = OrderLots();
   int digits = (int)MarketInfo(OrderSymbol(), MODE_DIGITS);
   double px = (OrderType() == OP_BUY) ? MarketInfo(OrderSymbol(), MODE_BID)
                                       : MarketInfo(OrderSymbol(), MODE_ASK);
   bool ok = OrderClose(ticket, NormalizeDouble(lots, 2),
                        NormalizeDouble(px, digits), InpMaxSlippagePoints, clrNONE);
   if(!ok)
   {
      Reply(id, false, "err=" + IntegerToString(GetLastError()) + ";msg=orderclose_failed");
      return;
   }
   Reply(id, true, "ticket=" + IntegerToString(ticket) + ";lots=" + Dbl(lots));
}

void CmdOrderModify(const string id, const string ps)
{
   if(!g_allow) { Reply(id, false, "err=trading_disabled;msg=allow_trading off (ini/input)"); return; }
   int ticket = (int)PNum(ps, "ticket", 0);
   if(!OrderSelect(ticket, SELECT_BY_TICKET, MODE_TRADES))
   {
      Reply(id, false, "err=ticket_not_found;msg=" + IntegerToString(GetLastError()));
      return;
   }
   int digits = (int)MarketInfo(OrderSymbol(), MODE_DIGITS);
   double sl = PNum(ps, "sl", OrderStopLoss());
   double tp = PNum(ps, "tp", OrderTakeProfit());
   bool ok = OrderModify(ticket, OrderOpenPrice(), NormalizeDouble(sl, digits),
                         NormalizeDouble(tp, digits), 0, clrNONE);
   if(!ok)
   {
      Reply(id, false, "err=" + IntegerToString(GetLastError()) + ";msg=ordermodify_failed");
      return;
   }
   Reply(id, true, "ticket=" + IntegerToString(ticket) + ";sl=" + Dbl(sl) + ";tp=" + Dbl(tp));
}

