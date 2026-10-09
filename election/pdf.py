from io import BytesIO
from xml.sax.saxutils import escape

from django.conf import settings
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import Paragraph

from .services import password_for


def credential_pdf(users):
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
        text = ('教會執事選舉 · 登入憑證<br/>'
                f'姓名：{escape(user.name)}（#{user.pk}）<br/>'
                f'<font size="19">密碼：{password_for(user)}</font><br/><br/>'
                f'{escape(settings.PUBLIC_URL)}<br/>'
                '開啟以上網址，只需輸入密碼登入。<br/>'
                '英文字母不分大小寫；最多選 10 位。<br/>'
                '請妥善保管憑證，勿交予他人。')
        paragraph = Paragraph(text, style)
        _, needed = paragraph.wrap(card_w - 26, card_h - 24)
        if needed > card_h - 24:
            compact = ParagraphStyle('compact', parent=style, fontSize=8, leading=11)
            paragraph = Paragraph(text, compact)
            _, needed = paragraph.wrap(card_w - 26, card_h - 24)
        if needed > card_h - 24:
            raise ValueError('憑證內容過長，請縮短網站網址或姓名。')
        paragraph.drawOn(pdf, x + 13, y + card_h - 12 - needed)
    pdf.save()
    return output.getvalue()
