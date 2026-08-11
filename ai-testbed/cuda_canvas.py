import torch
import cudacanvas


#REPLACE THIS with you training loop
while (True):

    #REPLACE THIS with you training code and generation of data
    noise_image = torch.rand((4, 500, 500), device="cuda")

    #Visualise your data in real-time
    cudacanvas.im_show(noise_image)

    #OPTIONAL: Terminate training when the window is closed
    if cudacanvas.should_close():
        cudacanvas.clean_up()
        #end process if the window is closed
        break

