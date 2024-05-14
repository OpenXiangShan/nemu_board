if [ -z "$(ls -A build)" ]; then
    echo "The build directory is empty. Creating the directory..."
    mkdir build
fi

dtc -O dtb -o build/xiangshan.dtb system.dts
