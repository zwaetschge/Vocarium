package ch.zwaetschge.vocarium

import android.content.Context
import android.graphics.*
import android.graphics.drawable.GradientDrawable
import android.graphics.drawable.RippleDrawable
import android.content.res.ColorStateList
import android.view.Gravity
import android.view.View
import android.widget.TextView

/** Small native visual vocabulary shared by library, reader and transport. */
object NativeDesign {
    val canvas = Color.rgb(12, 16, 25)
    val panel = Color.rgb(23, 30, 43)
    val border = Color.rgb(43, 53, 72)
    val ink = Color.rgb(242, 244, 250)
    val muted = Color.rgb(169, 180, 200)
    val accent = Color.rgb(174, 190, 255)
    val accentPanel = Color.rgb(43, 57, 94)
    fun dp(context: Context, value: Int) = (value * context.resources.displayMetrics.density).toInt()
    fun shape(context: Context, color: Int = panel, radius: Int = 16, outlined: Boolean = false) = GradientDrawable().apply {
        setColor(color); cornerRadius = dp(context, radius).toFloat()
        if (outlined) setStroke(dp(context, 1), border)
    }
    fun touch(context: Context, color: Int = panel, radius: Int = 16) = RippleDrawable(
        ColorStateList.valueOf(0x25FFFFFF), shape(context, color, radius), shape(context, Color.WHITE, radius))
    fun text(context: Context, value: String, size: Int = 16, color: Int = ink, bold: Boolean = false) = TextView(context).apply {
        text = value; textSize = size.toFloat(); setTextColor(color)
        typeface = Typeface.create("sans-serif", if (bold) Typeface.BOLD else Typeface.NORMAL)
        includeFontPadding = false
    }
}

/** Stroke icons are vectors at every density, without emoji or font substitutions. */
class NativeIcon(context: Context, private val kind: String, var tint: Int = NativeDesign.muted) : View(context) {
    private val p = Paint(Paint.ANTI_ALIAS_FLAG).apply { style = Paint.Style.STROKE; strokeWidth = 1.65f; strokeCap = Paint.Cap.ROUND; strokeJoin = Paint.Join.ROUND }
    override fun onDraw(canvas: Canvas) {
        super.onDraw(canvas)
        val side = minOf(width, height) * .52f
        canvas.save(); canvas.translate((width-side)/2, (height-side)/2); canvas.scale(side/24, side/24)
        p.color = tint
        fun line(vararg points: Float) { val path = Path(); path.moveTo(points[0], points[1]); var i=2; while(i<points.size){path.lineTo(points[i], points[i+1]);i+=2};canvas.drawPath(path,p) }
        when(kind) {
            "books" -> {canvas.drawRoundRect(3f,3f,9f,21f,1f,1f,p);canvas.drawRoundRect(11f,3f,17f,21f,1f,1f,p);line(20f,4f,23f,20f);line(4f,7f,8f,7f,8f,7f);line(12f,17f,16f,17f)}
            "podcasts" -> {canvas.drawRoundRect(8f,2f,16f,15f,4f,4f,p);canvas.drawArc(4f,5f,20f,19f,0f,180f,false,p);line(12f,19f,12f,23f,8f,23f,16f,23f)}
            "drama" -> {canvas.drawRoundRect(2f,4f,22f,20f,3f,3f,p);line(2f,9f,22f,9f);line(7f,4f,10f,9f);line(14f,4f,17f,9f);line(10f,12f,15f,15f,10f,18f,10f,12f)}
            "studio" -> {line(3f,9f,3f,15f);line(7.5f,5f,7.5f,19f);line(12f,2f,12f,22f);line(16.5f,7f,16.5f,17f);line(21f,10f,21f,14f)}
            "search" -> {canvas.drawCircle(10f,10f,7f,p);line(15f,15f,21f,21f)}
            "account" -> {canvas.drawCircle(12f,8f,4f,p);canvas.drawArc(4f,14f,20f,28f,180f,180f,false,p)}
            "back" -> line(15f,5f,8f,12f,15f,19f)
            "next" -> line(9f,5f,16f,12f,9f,19f)
            "play" -> {p.style=Paint.Style.FILL;line(8f,4f,20f,12f,8f,20f,8f,4f);p.style=Paint.Style.STROKE}
            "pause" -> {p.strokeWidth=4f;line(8f,5f,8f,19f);line(16f,5f,16f,19f);p.strokeWidth=1.65f}
            "more" -> {p.style=Paint.Style.FILL;for(x in listOf(5f,12f,19f))canvas.drawCircle(x,12f,1.8f,p);p.style=Paint.Style.STROKE}
            "plus" -> {line(12f,5f,12f,19f);line(5f,12f,19f,12f)}
            "list" -> {line(4f,6f,20f,6f);line(4f,12f,20f,12f);line(4f,18f,20f,18f)}
            "sleep" -> {line(5f,5f,11f,5f,5f,11f,11f,11f);line(13f,12f,20f,12f,13f,19f,20f,19f)}
            "people" -> {canvas.drawCircle(9f,8f,3.5f,p);canvas.drawArc(2f,14f,16f,26f,180f,180f,false,p);canvas.drawCircle(16.5f,9f,2.5f,p);canvas.drawArc(13f,15f,22f,24f,200f,160f,false,p)}
            "bookmark" -> {val path=Path();path.moveTo(6f,3f);path.lineTo(18f,3f);path.lineTo(18f,21f);path.lineTo(12f,16f);path.lineTo(6f,21f);path.close();canvas.drawPath(path,p)}
            "text" -> {line(4f,19f,9.5f,5f,15f,19f);line(6f,14f,13f,14f);line(15f,19f,18f,11f,21f,19f);line(16f,16.5f,20f,16.5f)}
            "sort" -> {line(4f,7f,16f,7f);line(4f,12f,12f,12f);line(4f,17f,8f,17f);line(18f,11f,18f,20f);line(15f,17f,18f,20f,21f,17f)}
            "download" -> {line(12f,3f,12f,15f);line(7f,10f,12f,15f,17f,10f);line(4f,19f,20f,19f)}
            "share" -> {canvas.drawCircle(18f,5f,2.5f,p);canvas.drawCircle(6f,12f,2.5f,p);canvas.drawCircle(18f,19f,2.5f,p);line(8.2f,10.8f,15.8f,6.2f);line(8.2f,13.2f,15.8f,17.8f)}
            "follow" -> {line(4f,5f,14f,5f);line(4f,10f,12f,10f);line(4f,15f,10f,15f);line(4f,20f,12f,20f);line(18f,8f,18f,20f);line(15f,17f,18f,20f,21f,17f)}
            "speed" -> {canvas.drawArc(3f,5f,21f,23f,180f,180f,false,p);line(12f,14f,16.5f,8.5f)}
        }
        canvas.restore()
    }
}

/** Actual server cover when available; title typography is the honest missing-cover state. */
class NativeCover(context: Context, title: String, seed: String) : View(context) {
    var bookTitle: String = title
        set(value) { field=value; invalidate() }
    var bitmap: Bitmap? = null
        set(value) { field=value; invalidate() }
    private val p = Paint(Paint.ANTI_ALIAS_FLAG)
    private val palettes = arrayOf(intArrayOf(0xFF3A485F.toInt(),0xFF182335.toInt()), intArrayOf(0xFF665C56.toInt(),0xFF302B32.toInt()),intArrayOf(0xFF365C59.toInt(),0xFF1B3036.toInt()),intArrayOf(0xFF655072.toInt(),0xFF2A243E.toInt()))
    private val colors = palettes[(seed.hashCode() and Int.MAX_VALUE)%palettes.size]
    init { importantForAccessibility = IMPORTANT_FOR_ACCESSIBILITY_NO }
    override fun onDraw(canvas: Canvas) {
        super.onDraw(canvas)
        val w=width.toFloat();val h=height.toFloat();val r=NativeDesign.dp(context,10).toFloat()
        val clip=Path().apply {addRoundRect(0f,0f,w,h,r,r,Path.Direction.CW)}
        canvas.save();canvas.clipPath(clip)
        val image=bitmap
        if(image!=null) {
            val scale=maxOf(w/image.width,h/image.height);val dw=image.width*scale;val dh=image.height*scale
            p.shader=null;canvas.drawBitmap(image,null,RectF((w-dw)/2,(h-dh)/2,(w+dw)/2,(h+dh)/2),p)
        } else {
            p.shader=LinearGradient(0f,0f,w,h,colors[0],colors[1],Shader.TileMode.CLAMP);canvas.drawRect(0f,0f,w,h,p);p.shader=null
            p.color=0x35FFFFFF;p.strokeWidth=1f;canvas.drawLine(w*.12f,h*.14f,w*.88f,h*.14f,p)
            p.color=Color.WHITE;p.typeface=Typeface.create("sans-serif",Typeface.BOLD);p.textSize=w*.125f
            val words=bookTitle.split(" ");val lines=mutableListOf<String>();var line=""
            for(word in words){val candidate=if(line.isEmpty())word else "$line $word";if(p.measureText(candidate)>w*.74f && line.isNotEmpty()){lines.add(line);line=word}else line=candidate};if(line.isNotEmpty())lines.add(line)
            for((i,t) in lines.take(4).withIndex()) canvas.drawText(t, w*.13f,h*.35f+i*w*.16f,p)
            p.color=0xBBFFFFFF.toInt();p.typeface=Typeface.create("sans-serif",Typeface.NORMAL);p.textSize=w*.062f;canvas.drawText("VOCARIUM",w*.13f,h*.87f,p)
        }
        p.shader=LinearGradient(0f,0f,w*.065f,0f,0x44000000,0x00000000,Shader.TileMode.CLAMP);canvas.drawRect(0f,0f,w*.065f,h,p);p.shader=null
        canvas.restore()
    }
}
