from io import BytesIO
from xml.sax.saxutils import escape

from django.conf import settings
from reportlab.graphics import renderPDF
from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.graphics.shapes import Drawing
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import Paragraph

from .services import password_for, voter_numbers


def credential_pdf(users):
    numbers = voter_numbers()
    if 'NotoTC' not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont('NotoTC', settings.PDF_FONT_PATH))
    output = BytesIO()
    pdf = canvas.Canvas(output, pagesize=A4)
    pdf.setTitle('教會執事選舉登入憑證')
    width, height = A4
    margin = 24
    card_w, card_h = (width - margin * 2) / 2, (height - margin * 2) / 4
    style = ParagraphStyle('card', fontName='NotoTC', fontSize=10, leading=14, autoLeading='max', wordWrap='CJK', textColor=colors.HexColor('#252132'))
    for index, user in enumerate(users):
        if index and index % 8 == 0:
            pdf.showPage()
        slot = index % 8
        x, y = margin + (slot % 2) * card_w, height - margin - (slot // 2 + 1) * card_h
        pdf.setStrokeColor(colors.HexColor('#b5adbd'))
        pdf.setDash(3, 3)
        pdf.rect(x, y, card_w, card_h)
        pdf.setDash()
        password = password_for(user)
        text = ('教會執事選舉 · 登入憑證<br/>'
                f'姓名：{escape(user.name)}（#{numbers[user.pk]}）<br/>'
                f'<font size="19">密碼：{password}</font><br/>'
                f'{escape(settings.PUBLIC_URL)}')
        paragraph = Paragraph(text, style)
        text_h = card_h - 108
        _, needed = paragraph.wrap(card_w - 26, text_h)
        if needed > text_h:
            compact = ParagraphStyle('compact', parent=style, fontSize=8, leading=11)
            paragraph = Paragraph(text, compact)
            _, needed = paragraph.wrap(card_w - 26, text_h)
        if needed > text_h:
            raise ValueError('憑證內容過長，請縮短網站網址或姓名。')
        paragraph.drawOn(pdf, x + 13, y + card_h - 12 - needed)
        qr = QrCodeWidget(f'{settings.PUBLIC_URL}/#password={password}', barWidth=72, barHeight=72)
        drawing = Drawing(72, 72)
        drawing.add(qr)
        renderPDF.draw(drawing, pdf, x + 13, y + 12)
        instructions = Paragraph('掃描 QR 碼即可登入，<br/>或開啟網址輸入密碼。<br/>最多選 10 位。<br/>請妥善保管憑證及 QR 碼，勿交予他人。', style)
        _, needed = instructions.wrap(card_w - 110, 84)
        if needed > 84:
            raise ValueError('憑證說明過長。')
        instructions.drawOn(pdf, x + 97, y + 96 - needed)
    pdf.save()
    return output.getvalue()
